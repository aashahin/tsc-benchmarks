import csv
import math
import random
import statistics
from collections import defaultdict

from .storage import read_journal, read_json, write_json


def distribution(samples):
    mean = statistics.mean(samples)
    deviation = statistics.stdev(samples) if len(samples) > 1 else 0
    return {"median": statistics.median(samples), "mean": mean, "min": min(samples), "max": max(samples),
            "stdev": deviation, "cv": deviation / mean if mean else 0}


def paired_speedup(ratios):
    logs = [math.log(ratio) for ratio in ratios]
    estimate = math.exp(statistics.mean(logs))
    if len(ratios) < 3:
        return {"estimate": estimate, "ci95": None}
    generator = random.Random(20261008)
    bootstraps = sorted(math.exp(statistics.mean(generator.choices(logs, k=len(logs)))) for _ in range(2000))
    return {"estimate": estimate, "ci95": [bootstraps[49], bootstraps[1949]]}


def summarize_group(pairs, attempts, requested):
    accepted = [pair for pair in pairs if pair["eligible"]]
    summary = {"completed_pairs": len(pairs), "valid_pairs": len(accepted), "requested_pairs": requested,
               "excluded_pairs": len(pairs) - len(accepted), "complete": len(accepted) >= requested}
    if not accepted:
        return summary
    samples = {"bun": [], "tsc": []}
    ratios = []
    for pair in accepted:
        measured = {attempts[identifier]["compiler"]: attempts[identifier] for identifier in pair["attempt_ids"]}
        for name in samples:
            samples[name].append(measured[name])
        ratios.append(measured["tsc"]["wall_seconds"] / measured["bun"]["wall_seconds"])
    for name, measurements in samples.items():
        summary[name] = {field: distribution([attempt[field] for attempt in measurements])
                         for field in ("wall_seconds", "user_seconds", "system_seconds", "peak_rss_bytes")}
    summary["speedup"] = paired_speedup(ratios)
    summary["noisy"] = any(summary[name]["wall_seconds"]["cv"] > 0.1 for name in samples)
    return summary


def build_summary(directory):
    metadata = read_json(directory / "metadata.json")
    validations = read_json(directory / "validation.json") if (directory / "validation.json").exists() else {}
    records = read_journal(directory / "raw.jsonl")
    attempts = {record["id"]: record for record in records if record["kind"] == "attempt"}
    groups = defaultdict(list)
    for record in records:
        if record["kind"] == "pair" and record["phase"] == "measurement":
            groups[(record["workload"], record["scenario"])].append(record)
    rows = []
    for workload in metadata["workloads"]:
        scenarios = ["full", "no-change", "leaf-edit", "api-edit"] if workload["category"] == "workflow" else ["full"]
        for scenario in scenarios:
            validation = validations.get(workload["id"], {"status": "pending"})
            row = {"workload": workload["id"], "category": workload["category"], "scenario": scenario,
                   "validation": validation["status"], "file_count": validation.get("file_count", 0),
                   **summarize_group(groups[(workload["id"], scenario)], attempts, metadata["settings"]["repetitions"])}
            rows.append(row)
    invalidated = metadata["status"] == "deferred-or-failed"
    aggregate = aggregate_speedups([] if invalidated else rows)
    return {"schema_version": 1, "metadata": metadata, "rows": rows, "aggregate": aggregate,
            "active_seconds": sum(attempt["wall_seconds"] for attempt in attempts.values()),
            "waited_seconds": sum(attempt["waited_seconds"] for attempt in attempts.values())}


def aggregate_speedups(rows):
    aggregates = {}
    for category in ("real-project", "stress"):
        eligible = [row for row in rows if row["category"] == category and row["scenario"] == "full"
                    and row["validation"] == "comparable" and row["complete"] and not row.get("noisy", True)]
        aggregates[category] = {"included_workloads": [row["workload"] for row in eligible],
                                "geometric_mean_speedup": statistics.geometric_mean(row["speedup"]["estimate"] for row in eligible) if eligible else None}
    return aggregates


def markdown_row(row):
    prefix = f"| {row['workload']} | {row['scenario']} | {row['valid_pairs']}/{row['requested_pairs']} |"
    if "speedup" not in row:
        return prefix + f" — | — | — | — | {row['validation']} |"
    bun, tsc = row["bun"], row["tsc"]
    status = "noisy" if row["noisy"] else "measured"
    if not row["complete"]:
        status += "; incomplete"
    rss = f"{bun['peak_rss_bytes']['median'] / 2**20:.0f} / {tsc['peak_rss_bytes']['median'] / 2**20:.0f}"
    confidence = row["speedup"]["ci95"]
    speed = f"{row['speedup']['estimate']:.2f}×"
    if confidence:
        speed += f" [{confidence[0]:.2f}, {confidence[1]:.2f}]"
    return prefix + f" {bun['wall_seconds']['median']:.3f} | {tsc['wall_seconds']['median']:.3f} | {speed} | {rss} | {status} |"


def write_markdown(directory, summary):
    metadata = summary["metadata"]
    lines = ["# Type-checker benchmark", "", f"Run: `{directory.name}`. Status: **{metadata['status']}**.", "",
             "These are measurements under laptop resource limits, with a warm filesystem cache. Speedup is TypeScript time / Bun time; above 1 favors Bun.", "",
             f"CPU affinity: `{metadata['limits']['cpus']}`; memory: {metadata['limits']['memory_bytes'] / 2**30:g} GiB; protection: {metadata['memory_mode']}.",
             f"Measured child-process time: {summary['active_seconds'] / 60:.1f} minutes. Capacity waits: {summary['waited_seconds'] / 60:.1f} minutes.", ""]
    for compiler in metadata["compilers"]:
        lines.append(f"- {compiler['name']}: `{compiler['version']}`, SHA-256 `{compiler['sha256']}`")
    lines += ["", "| Workload | Scenario | Valid pairs | Bun median (s) | TS median (s) | Paired speedup [95% CI] | Peak RSS MiB (Bun / TS) | Status |",
              "|---|---|---:|---:|---:|---:|---:|---|"]
    lines += [markdown_row(row) for row in summary["rows"]]
    lines += ["", "## Interpretation", "", "The confidence interval is a paired bootstrap estimate, not a correction for background load. `noisy` means wall-time coefficient of variation exceeds 10%. Incomplete, noisy, mismatching, or failing cases do not enter aggregates.", "",
              "Incremental rows include only the second command: TypeScript reuses its seeded cache; Bun checks again. Cache seeding and edits are outside the timed second command and recorded separately. These rows are not full-check speedups.", "",
              "Kernel peak RSS measures the native compiler; a separate 100 ms process-group sample drives the soft memory guard. With a soft guard, brief overshoots and swapping cannot be ruled out.", ""]
    for category, aggregate in summary["aggregate"].items():
        speed = aggregate["geometric_mean_speedup"]
        if speed is not None:
            lines.append(f"- {category}: {speed:.2f}× across {', '.join(aggregate['included_workloads'])}.")
        else:
            lines.append(f"- {category}: no aggregate; no complete, comparable, low-variance cases.")
    if metadata.get("reason"):
        lines += ["", f"Run note: {metadata['reason']}"]
    lines += ["", "See `validation.json` for diagnostics and coverage differences, `raw.jsonl` for every attempt, `metadata.json` for environment and source pins, and `logs/` for compiler output.", ""]
    (directory / "report.md").write_text("\n".join(lines))


def write_report(directory):
    summary = build_summary(directory)
    write_json(directory / "summary.json", summary)
    columns = ["workload", "category", "scenario", "validation", "file_count", "valid_pairs", "requested_pairs", "complete", "noisy",
               "bun_median_seconds", "tsc_median_seconds", "paired_speedup", "bun_peak_rss_bytes", "tsc_peak_rss_bytes"]
    with (directory / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in summary["rows"]:
            flat = {key: row.get(key) for key in columns}
            if "speedup" in row:
                flat["paired_speedup"] = row["speedup"]["estimate"]
                for name in ("bun", "tsc"):
                    flat[f"{name}_median_seconds"] = row[name]["wall_seconds"]["median"]
                    flat[f"{name}_peak_rss_bytes"] = row[name]["peak_rss_bytes"]["median"]
            writer.writerow(flat)
    write_markdown(directory, summary)
    return summary
