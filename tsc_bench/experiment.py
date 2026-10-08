import contextlib
import dataclasses
import hashlib
import random
import shutil
import uuid
from pathlib import Path

from .compilers import compiler_env
from .diagnostics import diagnostic_counts, file_list, log_text, parity, parse_diagnostics
from .resources import Command, measured_run, wait_for_capacity
from .storage import CACHE, ROOT, append_json, digest, read_journal, read_json, write_json


class BudgetExhausted(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class RunSettings:
    repetitions: int = 7
    warmups: int = 2
    seed: int = 20261008
    budget_seconds: float = 3600


def paired_order(seed, case, repetition):
    parity_bit = random.Random(f"{seed}:{case}").randrange(2)
    return ("bun", "tsc") if (repetition + parity_bit) % 2 else ("tsc", "bun")


def program_fingerprint(files, workload):
    hasher = hashlib.sha256()
    root = Path(workload["root"])
    candidates = {Path(workload["project"])}
    for filename in files:
        if filename.startswith("<project>/"):
            candidates.add(root / filename.removeprefix("<project>/"))
        elif filename.startswith("/"):
            candidates.add(Path(filename))
    for pattern in ("*lock*", "package.json", "tsconfig*.json"):
        candidates.update(root.glob(pattern))
    for path in sorted(candidates):
        if path.is_file():
            hasher.update(str(path).encode() + bytes.fromhex(digest(path)))
    return hasher.hexdigest()


@contextlib.contextmanager
def restored_edits(workload):
    root = Path(workload["root"])
    files = {root / "src/leaf.ts": (root / "src/leaf.ts").read_bytes(),
             root / "src/shared.ts": (root / "src/shared.ts").read_bytes()}
    try:
        yield files
    finally:
        for path, original in files.items():
            if path.read_bytes() != original:
                path.write_bytes(original)


def apply_edit(workload, scenario):
    root = Path(workload["root"])
    mutations = {"leaf-edit": (root / "src/leaf.ts", "return 1", "return 2"),
                 "api-edit": (root / "src/shared.ts", "revision?: number", "revision?: number; flag?: boolean")}
    if scenario in mutations:
        path, before, after = mutations[scenario]
        source = path.read_text()
        if source.count(before) != 1:
            raise RuntimeError("Workflow fixture differs from its recorded edit recipe")
        path.write_text(source.replace(before, after))


class Experiment:
    def __init__(self, directory, compilers, configuration):
        self.directory = directory
        self.compilers = {compiler.name: compiler for compiler in compilers}
        self.limits, self.settings = configuration
        self.launcher = CACHE / "launcher"
        self.launcher.mkdir(parents=True, exist_ok=True)
        self.journal = directory / "raw.jsonl"
        self.records = read_journal(self.journal)
        self.used_seconds = sum(record["wall_seconds"] for record in self.records if record["kind"] == "attempt")
        self.validations = read_json(directory / "validation.json") if (directory / "validation.json").exists() else {}

    def attempt(self, compiler, workload, request):
        scenario, phase, cache, extra = request
        remaining = self.settings.budget_seconds - self.used_seconds
        if remaining <= 0:
            raise BudgetExhausted("Active-run budget reached; resume to grant another budget")
        waited = wait_for_capacity(self.limits)
        identifier = uuid.uuid4().hex
        prefix = self.directory / "logs" / workload["id"] / f"{identifier}-{compiler.name}"
        command = Command(compiler.argv(Path(workload["project"]), len(self.limits.cpus), cache) + extra,
                          self.launcher, compiler_env(len(self.limits.cpus)), prefix)
        limits = dataclasses.replace(self.limits, timeout_seconds=min(remaining, self.limits.timeout_seconds))
        attempt = measured_run(command, limits)
        if attempt["status"] == "timeout" and remaining < self.limits.timeout_seconds:
            attempt["status"] = "budget_exhausted"
        attempt.update({"kind": "attempt", "id": identifier, "workload": workload["id"], "compiler": compiler.name,
                        "scenario": scenario, "phase": phase, "waited_seconds": waited})
        context = (Path(workload["root"]), self.launcher, self.compilers["tsc"].path.parent)
        attempt["diagnostics"] = parse_diagnostics(log_text(attempt), context)
        append_json(self.journal, attempt)
        self.records.append(attempt)
        self.used_seconds += attempt["wall_seconds"]
        if attempt["status"] == "interrupted":
            raise KeyboardInterrupt
        if attempt["status"] == "budget_exhausted":
            raise BudgetExhausted("Active-run budget reached during an attempt; partial output is saved")
        return attempt

    def validate(self, workload):
        comparisons = {}
        for compiler in self.compilers.values():
            coverage = self.attempt(compiler, workload, ("full", "coverage", None, ["--listFilesOnly"]))
            attempt = self.attempt(compiler, workload, ("full", "validation", None, []))
            context = (Path(workload["root"]), self.launcher, self.compilers["tsc"].path.parent)
            comparisons[compiler.name] = {"files": file_list(log_text(coverage), context),
                                           "diagnostics": attempt["diagnostics"], "attempt": attempt, "list_attempt": coverage}
        differences = parity(comparisons)
        expected = workload.get("expected_diagnostic_codes")
        if expected is not None:
            actual = sorted(diagnostic["code"] for diagnostic in comparisons["tsc"]["diagnostics"])
            if actual != sorted(expected):
                differences["expected_diagnostic_codes"] = {"expected": expected, "actual": actual}
        has_errors = any(comparison["diagnostics"] for comparison in comparisons.values())
        status = "matching-errors" if has_errors else "comparable"
        if expected is not None:
            status = "expected-errors"
        validation = {"status": "mismatch" if differences else status,
                      "differences": differences, "comparisons": comparisons}
        files = comparisons["tsc"]["files"]
        validation.update({"file_count": len(files), "fingerprint": program_fingerprint(files, workload),
                           "diagnostic_counts": {name: diagnostic_counts(comparison["diagnostics"]) for name, comparison in comparisons.items()}})
        previous = self.validations.get(workload["id"])
        if previous and "fingerprint" in previous and previous["fingerprint"] != validation["fingerprint"]:
            raise RuntimeError(f"Prepared source changed for {workload['id']}; start a new run")
        self.validations[workload["id"]] = validation
        write_json(self.directory / "validation.json", self.validations)
        print(f"  {workload['id']}: {validation['status']}, {len(files)} files", flush=True)
        return validation

    def validated_workloads(self, workloads):
        comparable = []
        for workload in workloads:
            if "error" in workload:
                self.validations[workload["id"]] = {"status": "unavailable", "reason": workload["error"]}
                continue
            validation = self.validate(workload)
            if validation["status"] == "comparable":
                comparable.append(workload)
        write_json(self.directory / "validation.json", self.validations)
        return comparable

    def workflow_attempt(self, compiler, workload, request):
        scenario, phase = request
        cache = self.directory / "incremental" / f"{compiler.name}.tsbuildinfo"
        cache.parent.mkdir(exist_ok=True)
        cache.unlink(missing_ok=True)
        with restored_edits(workload):
            seed = self.attempt(compiler, workload, (scenario, "seed", cache, []))
            if seed["status"] != "exited" or seed["exit_code"] != 0:
                raise RuntimeError(f"Incremental seed failed for {compiler.name}; see {seed['stderr']}")
            if compiler.name == "tsc" and not cache.exists():
                raise RuntimeError("TypeScript did not produce an incremental cache; refusing a misleading workflow result")
            apply_edit(workload, scenario)
            return self.attempt(compiler, workload, (scenario, phase, cache, []))

    def run_pair(self, workload, job):
        scenario, phase, repetition = job
        key = f"{workload['id']}:{scenario}:{phase}:{repetition}"
        if any(record.get("pair_id") == key for record in self.records if record["kind"] == "pair"):
            return
        attempts = []
        order = paired_order(self.settings.seed, workload["id"] + scenario + phase, repetition)
        for name in order:
            compiler = self.compilers[name]
            if scenario == "full":
                attempts.append(self.attempt(compiler, workload, (scenario, phase, None, [])))
            else:
                attempts.append(self.workflow_attempt(compiler, workload, (scenario, phase)))
        eligible = all(attempt["status"] == "exited" and attempt["exit_code"] == 0 and not attempt["diagnostics"] for attempt in attempts)
        pair = {"kind": "pair", "pair_id": key, "workload": workload["id"], "scenario": scenario,
                "phase": phase, "repetition": repetition, "order": order, "eligible": eligible,
                "attempt_ids": [attempt["id"] for attempt in attempts]}
        append_json(self.journal, pair)
        self.records.append(pair)
        timing = ", ".join(f"{attempt['compiler']} {attempt['wall_seconds']:.3f}s" for attempt in attempts)
        print(f"  {workload['id']} / {scenario} / {phase} {repetition + 1}: {timing}{'' if eligible else ' [excluded]'}", flush=True)

    def measure(self, workloads):
        for phase, count in (("warmup", self.settings.warmups), ("measurement", self.settings.repetitions)):
            for repetition in range(count):
                for workload in workloads:
                    scenarios = ["full", "no-change", "leaf-edit", "api-edit"] if workload["category"] == "workflow" else ["full"]
                    for scenario in scenarios:
                        self.run_pair(workload, (scenario, phase, repetition))

    def verify_unchanged(self, workloads):
        for workload in workloads:
            validation = self.validations[workload["id"]]
            files = validation["comparisons"]["tsc"]["files"]
            if program_fingerprint(files, workload) != validation["fingerprint"]:
                raise RuntimeError(f"Source changed while measuring {workload['id']}; results are invalidated")
        for compiler in self.compilers.values():
            if digest(compiler.path) != compiler.sha256:
                raise RuntimeError("A compiler changed while measuring; results are invalidated")


def isolated_workloads(directory, workloads):
    isolated = []
    for workload in workloads:
        if workload["category"] != "workflow" or "error" in workload:
            isolated.append(workload)
            continue
        target = directory / "fixtures" / workload["id"]
        if not target.exists():
            shutil.copytree(workload["root"], target)
        else:
            for relative in ("src/leaf.ts", "src/shared.ts"):
                shutil.copy2(Path(workload["root"]) / relative, target / relative)
        isolated.append({**workload, "root": str(target), "project": str(target / "tsconfig.json")})
    return isolated


def prepared_workloads(selected):
    path = CACHE / "prepared.json"
    prepared = read_json(path) if path.exists() else {}
    workloads = []
    for workload in selected:
        entry = prepared.get(workload["id"], {**workload, "error": "Not prepared; run prepare for this workload"})
        if "error" not in entry and entry["recipe_sha256"] != digest(ROOT / "workloads.json"):
            raise RuntimeError("Workload manifest changed; prepare again before benchmarking")
        workloads.append(entry)
    return workloads
