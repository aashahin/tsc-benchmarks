import dataclasses
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

GIB = 1024 ** 3


@dataclasses.dataclass(frozen=True)
class Limits:
    cpus: tuple[int, ...]
    memory_bytes: int = 6 * GIB
    start_free_bytes: int = 10 * GIB
    stop_free_bytes: int = 8 * GIB
    max_cpu_percent: float = 70
    timeout_seconds: float = 180
    pressure_wait_seconds: float = 300


@dataclasses.dataclass(frozen=True)
class Command:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_prefix: Path


def sibling_cpus(count):
    allowed = sorted(os.sched_getaffinity(0))
    for cpu in reversed(allowed):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
        siblings = expand_cpus(topology.read_text().strip()) if topology.exists() else [cpu]
        selected = sorted(set(siblings).intersection(allowed))
        if len(selected) >= count:
            return tuple(selected[:count])
    return tuple(allowed[:count])


def parse_cpus(spec):
    selected = expand_cpus(spec)
    if not selected or not set(selected).issubset(os.sched_getaffinity(0)):
        raise ValueError("CPU list must be nonempty and within the current affinity")
    return selected


def expand_cpus(spec):
    selected = set()
    for part in spec.split(","):
        bounds = [int(number) for number in part.split("-")]
        if len(bounds) > 2 or bounds[0] < 0 or bounds[-1] < bounds[0] or bounds[-1] >= os.cpu_count():
            raise ValueError("Invalid CPU list")
        selected.update(range(bounds[0], bounds[-1] + 1))
    return sorted(selected)


def available_memory():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Linux MemAvailable is required")


def cpu_counters():
    ticks = [int(tick) for tick in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(ticks), ticks[3] + ticks[4]


def cpu_percent(before, after):
    elapsed = after[0] - before[0]
    return 100 * (1 - (after[1] - before[1]) / elapsed) if elapsed else 0


def pressure_snapshot():
    snapshot = {"available_memory_bytes": available_memory()}
    for resource in ("cpu", "memory", "io"):
        path = Path(f"/proc/pressure/{resource}")
        snapshot[resource] = path.read_text().strip() if path.exists() else None
    return snapshot


def wait_for_capacity(limits):
    started = time.monotonic()
    before = cpu_counters()
    while True:
        time.sleep(0.25)
        after = cpu_counters()
        if available_memory() >= limits.start_free_bytes and cpu_percent(before, after) <= limits.max_cpu_percent:
            return time.monotonic() - started
        if time.monotonic() - started >= limits.pressure_wait_seconds:
            raise RuntimeError("Host remains busy; work deferred. Resume when the laptop has capacity.")
        if int(time.monotonic() - started) % 30 == 0:
            print("Waiting for laptop capacity…", file=sys.stderr, flush=True)
        time.sleep(2)
        before = after


def apply_priority(limits):
    os.sched_setaffinity(0, limits.cpus)
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=True, capture_output=True)


def cgroup_memory_mode(limits):
    group = Path("/proc/self/cgroup").read_text().strip().split("::")[-1]
    folder = Path("/sys/fs/cgroup") / group.lstrip("/")
    maximum = folder / "memory.max"
    swap = folder / "memory.swap.max"
    if maximum.exists() and maximum.read_text().strip().isdigit():
        if int(maximum.read_text()) <= limits.memory_bytes and swap.exists() and swap.read_text().strip() == "0":
            return "cgroup-hard-limit-plus-watchdog"
    return "sampled-process-tree-watchdog (soft limit)"


def maybe_enter_scope(limits):
    if "cgroup-hard" in cgroup_memory_mode(limits) or os.environ.get("TSC_BENCH_SCOPE_ATTEMPTED"):
        return
    if not shutil.which("systemd-run"):
        return
    probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"], capture_output=True)
    if probe.returncode:
        return
    scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
             f"--property=MemoryMax={limits.memory_bytes}", "--property=MemorySwapMax=0",
             "--property=CPUWeight=1", "--", sys.executable, *sys.argv]
    os.execvpe(scope[0], scope, {**os.environ, "TSC_BENCH_SCOPE_ATTEMPTED": "1"})


def group_rss(pgid):
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid:
                total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return total


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Watchdog:
    def __init__(self, pid, limits):
        self.pid = pid
        self.limits = limits
        self.finished = threading.Event()
        self.reason = None
        self.peak_tree_rss_bytes = 0
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        deadline = time.monotonic() + self.limits.timeout_seconds
        while not self.finished.wait(0.1):
            try:
                rss = group_rss(self.pid)
                self.peak_tree_rss_bytes = max(self.peak_tree_rss_bytes, rss)
                self.reason = self.abort_reason(rss, deadline)
            except (OSError, ValueError) as error:
                self.reason = f"monitor_failed: {error}"
            if self.reason:
                kill_group(self.pid)
                return

    def abort_reason(self, rss, deadline):
        if rss > self.limits.memory_bytes:
            return "memory_limit"
        if available_memory() < self.limits.stop_free_bytes:
            return "host_memory_pressure"
        if time.monotonic() >= deadline:
            return "timeout"
        return None


def measured_run(command, limits):
    command.log_prefix.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = command.log_prefix.with_suffix(".stdout")
    stderr_path = command.log_prefix.with_suffix(".stderr")
    before = pressure_snapshot()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        started = time.perf_counter_ns()
        child = subprocess.Popen(command.argv, cwd=command.cwd, env=command.env,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        watchdog = Watchdog(child.pid, limits)
        watchdog.thread.start()
        try:
            try:
                _, status, usage = os.wait4(child.pid, 0)
            except KeyboardInterrupt:
                watchdog.reason = "interrupted"
                kill_group(child.pid)
                _, status, usage = os.wait4(child.pid, 0)
            ended = time.perf_counter_ns()
            child.returncode = os.waitstatus_to_exitcode(status)
        except BaseException:
            kill_group(child.pid)
            child.wait()
            raise
        finally:
            watchdog.finished.set()
            watchdog.thread.join()
            kill_group(child.pid)
    limit_status = "memory_limit" if usage.ru_maxrss * 1024 > limits.memory_bytes else None
    return {
        "argv": command.argv, "cwd": str(command.cwd), "exit_code": child.returncode,
        "status": watchdog.reason or limit_status or ("exited" if child.returncode >= 0 else "signal"),
        "wall_seconds": (ended - started) / 1e9,
        "user_seconds": usage.ru_utime, "system_seconds": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "peak_tree_rss_bytes_sampled": watchdog.peak_tree_rss_bytes,
        "stdout": str(stdout_path), "stderr": str(stderr_path),
        "pressure_before": before, "pressure_after": pressure_snapshot(),
    }


def host_metadata():
    return {"platform": platform.platform(), "python": platform.python_version(),
            "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                               if line.startswith("model name")), platform.machine()),
            "logical_cpus": os.cpu_count(), "affinity": sorted(os.sched_getaffinity(0)),
            "pressure": pressure_snapshot()}
