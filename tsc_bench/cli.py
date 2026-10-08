import argparse
import dataclasses
import datetime
import json
import math
import platform
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from .compilers import inspect_compilers, load_compilers, pin_compilers
from .corpus import Preparation, prepare_all, select_workloads
from .experiment import BudgetExhausted, Experiment, RunSettings, isolated_workloads, prepared_workloads
from .report import write_report
from .resources import GIB, Limits, apply_priority, cgroup_memory_mode, host_metadata, maybe_enter_scope, parse_cpus, sibling_cpus
from .storage import CACHE, RESULTS, ROOT, digest, exclusive_lock, read_json, write_json


def parser():
    arguments = argparse.ArgumentParser(description="Compare bun check with native TypeScript 7+ under laptop resource limits.")
    commands = arguments.add_subparsers(dest="command", required=True)
    for name in ("doctor", "prepare", "validate", "run", "report"):
        command = commands.add_parser(name)
        if name == "report":
            command.add_argument("run_id")
            continue
        command.add_argument("--bun", type=Path, help="Path to Bun binary")
        command.add_argument("--tsc", type=Path, help="Path to native tsc binary (not the npm launcher)")
        command.add_argument("--workloads", help="Comma-separated IDs or all, offline, smoke, real, stress (default: all)")
        command.add_argument("--threads", type=int, choices=(1, 2))
        command.add_argument("--cpus", help="CPU affinity list; count must match --threads")
        command.add_argument("--memory-gib", type=float)
        command.add_argument("--timeout", type=float)
        command.add_argument("--pressure-wait", type=float)
        if name == "prepare":
            command.add_argument("--offline", action="store_true")
        if name in ("run", "validate"):
            command.add_argument("--repetitions", type=int)
            command.add_argument("--warmups", type=int)
            command.add_argument("--budget-minutes", type=float, default=60)
            command.add_argument("--seed", type=int)
            command.add_argument("--resume", help="Existing run ID; workload/settings/compilers are checked")
    return arguments


def apply_defaults(options):
    defaults = {"workloads": "all", "threads": 2, "memory_gib": 6, "timeout": 180,
                "pressure_wait": 300, "repetitions": 7, "warmups": 2, "seed": 20261008}
    if getattr(options, "resume", None):
        metadata = read_json(run_path(options.resume) / "metadata.json")
        limits = metadata["limits"]
        defaults.update({"workloads": ",".join(workload["id"] for workload in metadata["workloads"]),
                         "threads": len(limits["cpus"]), "cpus": ",".join(map(str, limits["cpus"])),
                         "memory_gib": limits["memory_bytes"] / GIB, "timeout": limits["timeout_seconds"],
                         "pressure_wait": limits["pressure_wait_seconds"]})
        defaults.update({field: metadata["settings"][field] for field in ("repetitions", "warmups", "seed")})
        defaults.update({compiler["name"]: Path(compiler["path"]) for compiler in metadata["compilers"]})
    for field, default in defaults.items():
        if hasattr(options, field) and getattr(options, field) is None:
            setattr(options, field, default)


def selected_limits(options):
    cpus = tuple(parse_cpus(options.cpus)) if options.cpus else sibling_cpus(options.threads)
    if len(cpus) != options.threads:
        raise ValueError("CPU affinity count must equal --threads")
    finite = all(math.isfinite(number) for number in (options.memory_gib, options.timeout, options.pressure_wait))
    if not finite or not 0 < options.memory_gib <= 6 or options.timeout <= 0 or options.pressure_wait <= 0:
        raise ValueError("Memory must be in (0, 6] GiB; timeout and pressure wait must be positive")
    return Limits(cpus, memory_bytes=int(options.memory_gib * GIB), timeout_seconds=options.timeout,
                  pressure_wait_seconds=options.pressure_wait)


def run_path(identifier):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Invalid run ID; use the results directory name")
    directory = RESULTS / identifier
    if not (directory / "metadata.json").exists():
        raise ValueError(f"No saved run named {identifier}")
    return directory


def doctor(options, limits):
    print(json.dumps({"host": host_metadata(), "limits": dataclasses.asdict(limits),
                      "memory_mode": cgroup_memory_mode(limits),
                      "tools": {name: shutil.which(name) for name in ("bun", "python3", "git", "ionice", "systemd-run")},
                      "workloads": [workload["id"] for workload in select_workloads(options.workloads)]}, indent=2))
    try:
        print(json.dumps({"compilers": [compiler.metadata() for compiler in load_compilers(options.bun, options.tsc)]}, indent=2))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def prepare(options, limits):
    try:
        compilers = load_compilers(options.bun, options.tsc)
    except RuntimeError:
        if options.offline or options.tsc:
            raise
        bun = options.bun or shutil.which("bun")
        if not bun:
            raise RuntimeError("Bun with native type checking is required for compiler preparation")
        destination = CACHE / "toolchains/typescript-package"
        preparation = Preparation([], limits, False)
        preparation.install_dependency(destination, "typescript", "7.0.2", bun=bun)
        compilers = inspect_compilers(options.bun, options.tsc)
    pinned = pin_compilers(compilers)
    failures = prepare_all(select_workloads(options.workloads), Preparation(pinned, limits, options.offline))
    print("Preparation complete" if not failures else f"Unavailable: {', '.join(failures)}")
    return 1 if failures else 0


def source_code_hash():
    import hashlib
    hasher = hashlib.sha256()
    for path in sorted((ROOT / "tsc_bench").glob("*.py")):
        hasher.update(path.name.encode() + bytes.fromhex(digest(path)))
    return hasher.hexdigest()


def new_metadata(options, compilers, limits):
    settings = RunSettings(options.repetitions, options.warmups, options.seed, options.budget_minutes * 60)
    if settings.repetitions < 1 or settings.warmups < 0 or not math.isfinite(settings.budget_seconds) or settings.budget_seconds <= 0:
        raise ValueError("Repetitions and budget must be positive; warmups cannot be negative")
    return {"schema_version": 1, "status": "running", "mode": options.command,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "host": host_metadata(), "limits": dataclasses.asdict(limits), "memory_mode": cgroup_memory_mode(limits),
            "compilers": [compiler.metadata() for compiler in compilers], "settings": dataclasses.asdict(settings),
            "harness_sha256": source_code_hash(), "workloads": prepared_workloads(select_workloads(options.workloads))}


def open_experiment(options, compilers, limits):
    incoming = new_metadata(options, compilers, limits)
    if options.resume:
        directory = run_path(options.resume)
        metadata = read_json(directory / "metadata.json")
        for field in ("compilers", "limits", "harness_sha256", "mode"):
            if json.dumps(metadata[field], sort_keys=True) != json.dumps(incoming[field], sort_keys=True):
                raise ValueError(f"Cannot resume with changed {field}; start a new run")
        for field in ("repetitions", "warmups", "seed"):
            if metadata["settings"][field] != incoming["settings"][field]:
                raise ValueError(f"Resume requires the original --{field}")
        if [workload["id"] for workload in metadata["workloads"]] != [workload["id"] for workload in incoming["workloads"]]:
            raise ValueError("Resume requires the original workload selection")
        incoming_workloads = {workload["id"]: workload for workload in incoming["workloads"]}
        metadata["workloads"] = [incoming_workloads[workload["id"]] if "error" in workload else workload for workload in metadata["workloads"]]
        metadata["status"] = "running"
        metadata.pop("reason", None)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = RESULTS / f"{stamp}-{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True)
        metadata = incoming
    write_json(directory / "metadata.json", metadata)
    experiment = Experiment(directory, compilers, (limits, RunSettings(**metadata["settings"])))
    if options.resume:
        experiment.settings = dataclasses.replace(experiment.settings, budget_seconds=experiment.used_seconds + options.budget_minutes * 60)
    return experiment, metadata


def execute(options, limits):
    compilers = load_compilers(options.bun, options.tsc)
    experiment, metadata = open_experiment(options, compilers, limits)
    print(f"Run ID: {experiment.directory.name}\nMemory protection: {metadata['memory_mode']}", flush=True)
    exit_code = 0
    try:
        workloads = isolated_workloads(experiment.directory, metadata["workloads"])
        comparable = experiment.validated_workloads(workloads)
        if options.command == "run":
            experiment.measure(comparable)
            experiment.verify_unchanged(comparable)
        accepted = {"comparable", "expected-errors"} if options.command == "validate" else {"comparable"}
        metadata["status"] = "finished" if all(case["status"] in accepted for case in experiment.validations.values()) else "finished-with-exclusions"
    except BudgetExhausted as error:
        metadata.update(status="budget-exhausted", reason=str(error))
        exit_code = 2
    except KeyboardInterrupt:
        metadata.update(status="interrupted", reason="Stopped by user; completed pairs are saved")
        exit_code = 130
    except (RuntimeError, OSError, ValueError) as error:
        metadata.update(status="deferred-or-failed", reason=str(error))
        exit_code = 1
    finally:
        metadata["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_json(experiment.directory / "metadata.json", metadata)
        summary = write_report(experiment.directory)
        print(f"Report: {experiment.directory / 'report.md'}", flush=True)
    accepted = {"comparable", "expected-errors"} if options.command == "validate" else {"comparable"}
    if any(row["validation"] not in accepted for row in summary["rows"]):
        exit_code = exit_code or 1
    if options.command == "run" and any(not row["complete"] for row in summary["rows"]):
        exit_code = exit_code or 1
    return exit_code


def main():
    options = parser().parse_args()
    try:
        if platform.system() != "Linux":
            raise RuntimeError("This runner requires Linux /proc and wait4 resource accounting")
        if options.command == "report":
            directory = run_path(options.run_id)
            with exclusive_lock():
                write_report(directory)
            print(directory / "report.md")
            return 0
        apply_defaults(options)
        limits = selected_limits(options)
        if options.command == "doctor":
            return doctor(options, limits)
        maybe_enter_scope(limits)
        apply_priority(limits)
        signal.signal(signal.SIGTERM, interrupt_run)
        with exclusive_lock():
            if options.command == "prepare":
                return prepare(options, limits)
            return execute(options, limits)
    except KeyboardInterrupt:
        print("Interrupted; completed preparation steps are saved", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def interrupt_run(_signum, _frame):
    raise KeyboardInterrupt
