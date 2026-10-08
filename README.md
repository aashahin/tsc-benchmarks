# Bun check vs tsgo (TypeScript 7)

## Result: Bun wins full checks; tsgo wins some cached checks

**Bun was faster in all three measured full type checks.** Its advantage ranged from **1.69× to 7.91×**, based on median elapsed time.

**tsgo was faster when reusing its cache for unchanged code or a leaf-function edit.** Bun was faster after a shared-interface edit.

**Bun used 32–66% less peak memory in full checks.** For cached checks, tsgo used about 5% less with unchanged code and 1% less after a leaf edit; Bun used 22% less after the shared-interface edit.

Here, **tsgo** means Microsoft's Go-based native TypeScript **7.0.2** compiler. Its released command is named `tsc`; it is not the older JavaScript compiler. The Bun version tested was **1.4.3-canary.1 (`620b50f6a`)**.

### Full check: both compilers check everything

Incremental caches are disabled. Lower time is better.

| Test | Bun | tsgo | Result |
|---|---:|---:|---|
| Zod objects | 2.687 s | 21.254 s | **Bun is 7.91× faster** |
| Zod chains | 1.499 s | 3.659 s | **Bun is 2.44× faster** |
| Generated 1,000-module project | 0.617 s | 1.042 s | **Bun is 1.69× faster** |

### Repeat check: tsgo uses its cache; Bun checks again

These tests use the same generated project. An untimed first check prepares the cache; the table measures the next check.

| Test | Bun | tsgo | Result |
|---|---:|---:|---|
| No code changes | 0.633 s | 0.456 s | **tsgo is 1.39× faster** |
| Leaf function body changed | 0.608 s | 0.386 s | **tsgo is 1.58× faster** |
| Shared interface changed | 0.610 s | 0.799 s | **Bun is 1.31× faster** |

Times are medians of seven measured runs per compiler, after two warmup pairs. **“× faster” is the slower median divided by the faster median.** The JSON also provides separately labelled paired statistics.

**Scope:** these are two Zod-generated stress workloads and one generated TypeScript project. **VS Code, Sentry, Playwright, Excalidraw, TypeORM, and tRPC were not measured.** All measured scenarios had more than 10% timing variability on a busy laptop, so the size of the advantage may change on another run or machine.

See the [84 individual samples](benchmarks/2026-10-08-laptop/samples.csv) and [full statistics](benchmarks/2026-10-08-laptop/summary.json).

### Memory: Bun uses less in full checks

Median peak resident memory (RSS) across seven measured runs per compiler; lower is better. This measures each compiler process’s peak RAM use during a check. It excludes setup and the on-disk incremental cache.

| Workload / scenario | Bun | tsgo | Result |
|---|---:|---:|---|
| Zod objects / full check | 237.5 MiB | 494.9 MiB | **Bun uses 52% less** |
| Zod chains / full check | 135.2 MiB | 399.5 MiB | **Bun uses 66% less** |
| Generated project / full check | 85.5 MiB | 125.8 MiB | **Bun uses 32% less** |
| Generated project / unchanged | 86.1 MiB | 81.6 MiB | tsgo uses 5% less |
| Generated project / leaf edit | 85.1 MiB | 84.5 MiB | Nearly equal: tsgo uses 1% less |
| Generated project / shared-interface edit | 84.0 MiB | 108.3 MiB | **Bun uses 22% less** |

Percentages compare the unrounded medians. The leaf-edit difference is only about 0.6 MiB, smaller than the observed run-to-run memory variation.

## What was actually benchmarked

This Bun build reports TypeScript 7.0.2 as its compatibility version.

**Three workloads were measured.** Two reuse Zod’s open-source benchmark generator; the third is a generated TypeScript project. These are stress tests, not measurements of complete application repositories.

| Workload | What the checker receives | Origin |
|---|---|---|
| Zod objects | 2,000 base object schemas with 16 fields each, plus `.omit()`, `.pick()`, and `.extend()` variants | [Zod’s generator](vendor/zod/generate.ts), using Zod 4.4.3 |
| Zod chains | 128 independent chains, with 25 schema stages per chain and 12 initial fields | The same upstream Zod generator |
| Generated project | 1,000 consumer modules importing a shared interface, plus a separate leaf function: 1,002 source files | This repository’s [fixture generator](tsc_bench/corpus.py) |

The generated project was tested four ways: a full check, an unchanged repeat check, a leaf-function edit, and a shared-interface edit. That makes **six scenarios**, each measured seven times with both compilers: **42 pairs, or 84 timed compiler invocations**. Every measured pair completed without type errors.

**Application results are still missing.** VS Code, Sentry, Playwright, Excalidraw, TypeORM, and tRPC could not be downloaded because the execution environment blocked network access. Their setup recipes are included, but this README makes no performance claim about them.

## Test conditions

Recorded on **October 8, 2026**, on Linux with an Intel Core i7-6820HQ. Both compilers used **two logical CPUs on one physical core**, low CPU/I/O priority, and a 6 GiB soft memory watchdog. Other work was running on the laptop.

## Data and excluded workloads

- [Individual samples](benchmarks/2026-10-08-laptop/samples.csv): all 84 timed invocations, with their pair, compiler, elapsed time, CPU time, and peak memory.
- [Results and metadata](benchmarks/2026-10-08-laptop/summary.json): exact compiler hashes, settings, statistics, confidence intervals, and exclusions. Its speedup statistics use paired ratios; the README headline compares median times.
- [Workload definitions](workloads.json) and [source attribution](THIRD_PARTY.md): pinned commits and generation parameters.

The historical Microsoft compiler-union fixture was also checked. Both compilers produced the same 35 diagnostics, so it was excluded from performance measurements. The six unavailable applications have no timing samples. Public result files contain no user-directory paths or raw process logs.

## Run the benchmarks

Requires Linux, Python 3.11+, Bun with native `bun check`, Git, and `ionice`. The Python runner uses only the standard library. Bun runs JavaScript/TypeScript setup scripts and installs dependencies. Native TypeScript 7 is installed or reused as a benchmark subject during preparation.

From the repository root, reproduce the measured workloads:

```sh
bun run --bun doctor
bun run --bun prepare --workloads zod-objects,zod-chains,edit-graph
bun run --bun bench --workloads zod-objects,zod-chains,edit-graph
```

To prepare and benchmark the six application repositories when network access is available:

```sh
bun run --bun prepare --workloads real
bun run --bun bench --workloads real
```

To select the complete configured suite, omit `--workloads`. Preparation pins source revisions and installs dependencies before timing. It disables unnecessary browser/Electron downloads. `--offline` can reuse prepared repositories and cached compiler/Zod packages.

Application dependencies use `bun install --frozen-lockfile --ignore-scripts`. When an upstream repository has a supported npm, Yarn v1, or pnpm lockfile, preparation first converts it with `bun pm migrate`. Unsupported lockfiles or Bun compatibility errors leave the workload unavailable with an explanation. Application preparation has not been validated in this network-restricted environment. See [Bun’s lockfile migration support](https://bun.com/docs/pm/lockfile).

### Laptop limits

The defaults prioritize keeping the machine usable:

- Run one setup or benchmark command at a time, on at most two logical CPUs.
- Use niceness 19 and idle I/O priority.
- Limit memory to 6 GiB. Use a systemd memory limit when available; otherwise use a sampled watchdog with a soft limit.
- Wait if available memory is below 10 GiB or host CPU usage exceeds 70%; abort a running compiler if available memory falls below 8 GiB.
- Limit each compiler invocation to three minutes and each run to 60 minutes of active subprocess time. Preparation and capacity waits add to elapsed time.

The recorded run used the soft watchdog. It cannot guarantee that memory never briefly exceeds its limit. CPU or memory pressure can defer a run without losing completed pairs.

For a smaller resource allowance:

```sh
bun run --bun bench --workloads zod-objects,zod-chains --threads 1 --memory-gib 4
```

### Stop, resume, and inspect

The runner prints a run ID and saves reports under `results/`, which is ignored by Git. Stop with Ctrl-C; completed pairs are retained. Replace `RUN_ID` below with the printed ID:

```sh
bun run --bun bench --resume RUN_ID --budget-minutes 30
bun run --bun report RUN_ID
```

Reports include Markdown, JSON, CSV, diagnostic comparisons, and per-command logs. Only the reviewed exports under `benchmarks/` are included in this repository. Changed compilers, harness code, limits, or source fingerprints require a new run.

A run with unavailable, incompatible, or incomplete cases exits with code 1 and still writes its report. Budget exhaustion returns 2; interruption returns 130.

## How measurements are collected

1. Compare the file lists and normalized diagnostics of both checkers. Empty programs, mismatches, type errors, crashes, and failed discovery are excluded from timing.
2. Run two warmup pairs, then seven measured pairs per scenario. Alternate the compiler order using a recorded seed.
3. Launch native compiler processes directly. Measure elapsed time with a monotonic clock and CPU/peak memory with Linux `wait4`. Redirect compiler output to files.
4. Preserve all attempts. Only complete, successful pairs enter the reported timings. Noise flags exclude cases from aggregate speedups.

Filesystem caches are warm and are never flushed. Source generation, dependency installation, incremental-cache seeding, and edits are outside the corresponding timed check. Repeated-edit tests restore their source and cache state between trials. Confidence intervals in the JSON are paired bootstrap estimates; they do not remove background-load bias.

This suite measures **type checking**. It does not measure JavaScript execution, bundling, declaration emission, editor responsiveness, or maximum all-core throughput.

## Check the benchmark code

The project’s own TypeScript checks use Bun. TypeScript 7 is invoked only as a comparison subject and for its benchmark fixture/cache preparation.

```sh
bun install --frozen-lockfile --ignore-scripts
bun run --bun typecheck
bun run --bun test
```

The tests cover resource limits, process cleanup, diagnostic parsing, statistics, source integrity, and interruption/resume behavior. The small `smoke` workload group separately checks valid code, deliberate errors, and project references against the benchmark subjects.
