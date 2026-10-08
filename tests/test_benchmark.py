import dataclasses
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tsc_bench.compilers import Compiler, compiler_env, load_compilers
from tsc_bench.corpus import Preparation, base_config, generate_edit_graph, jsonc, select_workloads, verify_vendor
from tsc_bench.diagnostics import canonical_path, file_list, parse_diagnostics
from tsc_bench.experiment import Experiment, RunSettings, apply_edit, paired_order, program_fingerprint, restored_edits
from tsc_bench.report import aggregate_speedups, paired_speedup, summarize_group
from tsc_bench.resources import Command, GIB, Limits, measured_run, sibling_cpus
from tsc_bench.storage import ROOT, append_json, read_journal, write_json


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.limits = Limits(sibling_cpus(1), start_free_bytes=0, stop_free_bytes=0, timeout_seconds=3)

    def command(self, script):
        return Command([sys.executable, "-c", script], self.folder, os.environ.copy(), self.folder / "child")

    def test_process_records_wall_time_cpu_time_and_kernel_peak_memory(self):
        attempt = measured_run(self.command("print('measured'); allocated=bytearray(12*1024*1024)"), self.limits)
        self.assertEqual(attempt["status"], "exited")
        self.assertEqual(attempt["exit_code"], 0)
        self.assertGreater(attempt["peak_rss_bytes"], 12 * 1024**2)
        self.assertGreater(attempt["user_seconds"] + attempt["system_seconds"], 0)
        self.assertIn("measured", Path(attempt["stdout"]).read_text())
        self.assertGreater(attempt["wall_seconds"], 0)

    def test_timeout_kills_descendant_process_group(self):
        script = "import subprocess,time; child=subprocess.Popen(['sleep','60']); print(child.pid,flush=True); time.sleep(60)"
        attempt = measured_run(self.command(script), dataclasses.replace(self.limits, timeout_seconds=0.25))
        self.assertEqual(attempt["status"], "timeout")
        self.assertLess(attempt["wall_seconds"], 3)
        pid = int(Path(attempt["stdout"]).read_text())
        time.sleep(0.1)
        stat = Path(f"/proc/{pid}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")

    def test_memory_limit_excludes_large_allocation(self):
        attempt = measured_run(self.command("import time; allocation=bytearray(64*1024*1024); time.sleep(10)"),
                               dataclasses.replace(self.limits, memory_bytes=32 * 1024**2))
        self.assertEqual(attempt["status"], "memory_limit")
        self.assertLess(attempt["wall_seconds"], 3)

    def test_host_memory_pressure_aborts_even_small_process(self):
        # Simulate changing /proc memory at the sensor boundary, not compiler behavior.
        with patch("tsc_bench.resources.available_memory", return_value=1):
            attempt = measured_run(self.command("import time; time.sleep(10)"),
                                   dataclasses.replace(self.limits, stop_free_bytes=GIB))
        self.assertEqual(attempt["status"], "host_memory_pressure")

    def test_crash_and_nonzero_exit_are_not_success(self):
        for script, expected in (("raise SystemExit(23)", "exited"),
                                 ("import os,signal; os.kill(os.getpid(),signal.SIGTERM)", "signal")):
            with self.subTest(script=script):
                attempt = measured_run(self.command(script), self.limits)
                self.assertEqual(attempt["status"], expected)
                self.assertNotEqual(attempt["exit_code"], 0)


class DiagnosticTests(unittest.TestCase):
    def test_multiline_errors_and_duplicate_diagnostics_are_retained(self):
        context = (Path("/tmp/project"), Path("/tmp/launcher"))
        message = "../project/a.ts(1,2): error TS2322: Type 'A' is not assignable.\n  More details.\n"
        parsed = parse_diagnostics(message * 2 + "Found 2 errors\n", context)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["file"], "<project>/a.ts")
        self.assertEqual(parsed[0]["message"], "Type 'A' is not assignable. More details.")

    def test_bun_error_summary_is_not_part_of_last_diagnostic(self):
        message = "a.ts(1,2): error TS2322: Invalid assignment.\n\nFound 3 errors\n\nErrors Files\n  3 a.ts:1\n"
        parsed = parse_diagnostics(message, (Path("/tmp"), Path("/tmp")))
        self.assertEqual(parsed[0]["message"], "Invalid assignment.")

    def test_standard_library_aliases_match_but_project_paths_remain_distinct(self):
        context = (Path("/tmp/project"), Path("/tmp/launcher"), Path("/tmp/typescript/lib"))
        self.assertEqual(canonical_path("bundled:///libs/lib.es5.d.ts", context),
                         canonical_path("/tmp/typescript/lib/lib.es5.d.ts", context))
        self.assertNotEqual(canonical_path("/tmp/project/lib.es5.d.ts", context),
                            canonical_path("bundled:///libs/lib.es5.d.ts", context))
        self.assertNotEqual(canonical_path("/tmp/project/typescript/lib.es5.d.ts", context),
                            canonical_path("bundled:///libs/lib.es5.d.ts", context))
        self.assertEqual(file_list("../project/a.ts\n/tmp/project/b.ts\nNo type errors\n", context),
                         ["<project>/a.ts", "<project>/b.ts"])

    def test_configuration_errors_without_file_positions_are_parsed(self):
        parsed = parse_diagnostics("error TS18003: No inputs were found.\n", (Path("/tmp"), Path("/tmp")))
        self.assertEqual([(entry["code"], entry["file"]) for entry in parsed], [(18003, "<config>")])


class ReproducibilityTests(unittest.TestCase):
    def test_upstream_corpus_matches_committed_git_blob_hashes(self):
        for vendor in ("compiler-unions", "zod"):
            with self.subTest(vendor=vendor):
                self.assertEqual(len(verify_vendor(ROOT / "vendor" / vendor)["commit"]), 40)

    def test_jsonc_preserves_comment_like_strings_and_removes_trailing_commas(self):
        self.assertEqual(jsonc('{"url":"https://a/*b*/",/* remove */"list":[1,],// comment\n}'),
                         {"url": "https://a/*b*/", "list": [1]})

    def test_pair_order_is_reproducible_and_balanced(self):
        orders = [paired_order(123, "case", repetition) for repetition in range(8)]
        self.assertEqual(orders.count(("bun", "tsc")), 4)
        self.assertEqual(orders, [paired_order(123, "case", repetition) for repetition in range(8)])

    def test_invalid_selection_never_silently_runs_everything(self):
        with self.assertRaises(ValueError):
            select_workloads("typo")
        self.assertEqual(len(select_workloads("smoke,smoke")), 3)

    def test_interrupted_edit_restores_fixture_and_detects_changed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            generate_edit_graph(folder, 2)
            write_json(folder / "tsconfig.json", base_config())
            workload = {"root": str(folder), "project": str(folder / "tsconfig.json")}
            files = ["<project>/src/shared.ts", "<project>/src/leaf.ts"]
            before = program_fingerprint(files, workload)
            with self.assertRaises(KeyboardInterrupt):
                with restored_edits(workload):
                    apply_edit(workload, "api-edit")
                    self.assertNotEqual(program_fingerprint(files, workload), before)
                    raise KeyboardInterrupt
            self.assertEqual(program_fingerprint(files, workload), before)

    def test_truncated_journal_is_rejected_instead_of_losing_saved_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "raw.jsonl"
            append_json(journal, {"kind": "pair", "pair_id": "a"})
            self.assertEqual(len(read_journal(journal)), 1)
            with journal.open("a") as stream:
                stream.write('{"kind":')
            with self.assertRaises(ValueError):
                read_journal(journal)


class PreparationTests(unittest.TestCase):
    def test_unsupported_locks_stop_before_installation(self):
        preparation = Preparation([], Limits(sibling_cpus(1)), False)
        for name, contents, explanation in (
            ("package-lock.json", '{"lockfileVersion": 1}', "npm lockfile v1"),
            ("yarn.lock", "__metadata:\n  version: 8\n", "Yarn v1"),
        ):
            with self.subTest(lock=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / name).write_text(contents)
                with self.assertRaisesRegex(RuntimeError, explanation):
                    preparation.check_migration(folder)

    def test_bun_migration_preserves_upstream_lock_and_skips_scripts(self):
        bun = shutil.which("bun")
        if not bun:
            self.skipTest("Bun is required for dependency preparation integration")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "fixture-dependency").mkdir()
            write_json(folder / "fixture-dependency/package.json", {"name": "fixture-dependency", "version": "1.0.0"})
            dependencies = {"fixture-dependency": "file:fixture-dependency"}
            write_json(folder / "package.json", {"name": "migration-fixture", "version": "1.0.0",
                       "dependencies": dependencies, "scripts": {"postinstall": "touch script-was-run"}})
            write_json(folder / "package-lock.json", {"name": "migration-fixture", "version": "1.0.0",
                       "lockfileVersion": 3, "packages": {
                           "": {"name": "migration-fixture", "version": "1.0.0", "dependencies": dependencies},
                           "fixture-dependency": {"name": "fixture-dependency", "version": "1.0.0"},
                           "node_modules/fixture-dependency": {"resolved": "fixture-dependency", "link": True}}})
            before = (folder / "package-lock.json").read_bytes()
            compiler = Compiler("bun", Path(bun), "test", "test", "test")
            limits = Limits(sibling_cpus(1), start_free_bytes=0, stop_free_bytes=0, max_cpu_percent=100,
                            memory_bytes=256 * 1024**2)
            with patch("tsc_bench.corpus.CACHE", folder / "cache"):
                installation = Preparation([compiler], limits, False).install(folder)
            self.assertEqual((folder / "package-lock.json").read_bytes(), before)
            self.assertTrue((folder / installation["lockfile"]).is_file())
            self.assertTrue((folder / "node_modules/fixture-dependency/package.json").is_file())
            self.assertFalse((folder / "script-was-run").exists())


class ReportingTests(unittest.TestCase):
    def test_constant_paired_speedup_has_exact_confidence_interval(self):
        summary = paired_speedup([2.0] * 7)
        self.assertAlmostEqual(summary["estimate"], 2.0)
        self.assertEqual(summary["ci95"], [2.0, 2.0])
        self.assertIsNone(paired_speedup([2.0])["ci95"])

    def test_failed_pairs_and_partial_results_cannot_enter_aggregate(self):
        summaries = [{"workload": "valid", "category": "stress", "scenario": "full", "validation": "comparable",
                      "complete": True, "noisy": False, "speedup": {"estimate": 2.0}}]
        for excluded in ({"complete": False}, {"noisy": True}, {"validation": "mismatch"}):
            summaries.append({**summaries[0], **excluded, "workload": "excluded", "speedup": {"estimate": 100.0}})
        aggregate = aggregate_speedups(summaries)
        self.assertEqual(aggregate["stress"]["included_workloads"], ["valid"])
        self.assertEqual(aggregate["stress"]["geometric_mean_speedup"], 2.0)
        empty = summarize_group([{"eligible": False}], {}, 7)
        self.assertFalse(empty["complete"])
        self.assertEqual(empty["excluded_pairs"], 1)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        executable = self.folder / "compiler"
        executable.write_text(f"#!{sys.executable}\nprint('checked')\n")
        executable.chmod(0o755)
        self.compilers = [Compiler(name, executable, "test", "test", "test") for name in ("bun", "tsc")]
        self.limits = Limits(sibling_cpus(1), start_free_bytes=0, stop_free_bytes=0, max_cpu_percent=100)
        self.settings = RunSettings(repetitions=2, warmups=0, budget_seconds=30)
        self.workload = {"id": "fixture", "category": "stress", "root": str(self.folder),
                         "project": str(self.folder / "tsconfig.json")}

    def experiment(self):
        return Experiment(self.folder, self.compilers, (self.limits, self.settings))

    def test_resume_does_not_repeat_completed_pairs(self):
        self.experiment().measure([self.workload])
        original = (self.folder / "raw.jsonl").read_bytes()
        self.experiment().measure([self.workload])
        self.assertEqual((self.folder / "raw.jsonl").read_bytes(), original)

    def test_resume_restarts_uncommitted_pair_without_losing_partial_attempt(self):
        first = self.experiment()
        first.attempt(self.compilers[0], self.workload, ("full", "measurement", None, []))
        resumed = self.experiment()
        resumed.measure([self.workload])
        records = read_journal(self.folder / "raw.jsonl")
        self.assertEqual(sum(record["kind"] == "attempt" for record in records), 5)
        self.assertEqual(sum(record["kind"] == "pair" for record in records), 2)

    def test_changed_source_is_not_silently_accepted_on_resume(self):
        source = self.folder / "source.ts"
        source.write_text("export const count = 1;\n")
        write_json(self.folder / "tsconfig.json", {"files": ["source.ts"]})
        files = ["<project>/source.ts"]
        first = self.experiment()
        first.validations["fixture"] = {"fingerprint": program_fingerprint(files, self.workload),
                                         "comparisons": {"tsc": {"files": files}}}
        source.write_text("export const count = 2;\n")
        with self.assertRaisesRegex(RuntimeError, "Source changed"):
            first.verify_unchanged([self.workload])


class NativeCompilerTests(unittest.TestCase):
    def test_bun_finds_intentional_error_without_running_package_check_script(self):
        try:
            compilers = [compiler for compiler in load_compilers() if compiler.name == "bun"]
        except (RuntimeError, FileNotFoundError) as error:
            self.skipTest(f"Native compiler integration requires prepared toolchains: {error}")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            project = folder / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src/index.ts").write_text('export const count: number = "invalid";\n')
            write_json(project / "tsconfig.json", base_config())
            write_json(project / "package.json", {"scripts": {"check": "exit 91"}})
            for compiler in compilers:
                command = Command(compiler.argv(project / "tsconfig.json", 1), folder, compiler_env(1), folder / compiler.name)
                limits = Limits(sibling_cpus(1), start_free_bytes=0, stop_free_bytes=0,
                                memory_bytes=256 * 1024**2, timeout_seconds=15)
                attempt = measured_run(command, limits)
                self.assertEqual(attempt["exit_code"], 1, attempt)
                diagnostics = parse_diagnostics(Path(attempt["stdout"]).read_text(), (project, folder))
                self.assertEqual([entry["code"] for entry in diagnostics], [2322])


if __name__ == "__main__":
    unittest.main()
