import collections
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR = re.compile(r"^(?:(.*?)\((\d+),(\d+)\):\s*)?error TS(\d+):\s*(.*)$")
LIB = re.compile(r"(?:^|[/\\])lib\.[^/\\]+\.d\.ts$")


def canonical_path(filename, context):
    project_root, cwd, *library_roots = context
    filename = filename.replace("\\", "/")
    if filename.startswith("bundled:///libs/"):
        return "<typescript-lib>/" + filename.rsplit("/", 1)[-1]
    absolute = (cwd / filename).resolve() if not filename.startswith("/") else Path(filename).resolve()
    if LIB.search(filename) and absolute.parent in {root.resolve() for root in library_roots}:
        return "<typescript-lib>/" + absolute.name
    if absolute.is_relative_to(project_root.resolve()):
        return "<project>/" + absolute.relative_to(project_root.resolve()).as_posix()
    return str(absolute)


def parse_diagnostics(text, context):
    diagnostics = []
    continuing = False
    for raw_line in ANSI.sub("", text).splitlines():
        match = ERROR.match(raw_line)
        if match:
            continuing = True
            filename, line, column, code, message = match.groups()
            diagnostics.append({"file": canonical_path(filename, context) if filename else "<config>",
                                "line": int(line or 0), "column": int(column or 0),
                                "code": int(code), "message": " ".join(message.split())})
        elif continuing and raw_line.startswith("  ") and raw_line.strip():
            diagnostics[-1]["message"] += " " + " ".join(raw_line.split())
        else:
            continuing = False
    return sorted(diagnostics, key=lambda diagnostic: tuple(diagnostic.values()))


def file_list(text, context):
    paths = []
    for line in ANSI.sub("", text).splitlines():
        filename = line.strip()
        if filename.startswith(("/", "./", "../", "bundled:///")) and re.search(r"\.(?:[cm]?[jt]sx?|json)$", filename):
            paths.append(canonical_path(filename, context))
    return sorted(set(paths))


def log_text(attempt):
    return "\n".join(Path(attempt[channel]).read_text(errors="replace") for channel in ("stdout", "stderr"))


def parity(comparisons):
    bun, tsc = comparisons["bun"], comparisons["tsc"]
    differences = {}
    for field in ("files", "diagnostics"):
        if bun[field] != tsc[field]:
            differences[field] = {"bun": bun[field], "tsc": tsc[field]}
    if any(not comparison["files"] for comparison in comparisons.values()):
        differences["empty_program"] = True
    for name, comparison in comparisons.items():
        attempt = comparison["attempt"]
        if attempt["status"] != "exited" or attempt["exit_code"] not in (0, 1, 2):
            differences[name + "_execution"] = attempt["status"]
        if attempt["exit_code"] != 0 and not comparison["diagnostics"]:
            differences[name + "_unparsed_failure"] = attempt["exit_code"]
        if comparison["list_attempt"]["exit_code"] != 0:
            differences[name + "_file_discovery_failed"] = True
    return differences


def diagnostic_counts(diagnostics):
    return dict(collections.Counter(str(diagnostic["code"]) for diagnostic in diagnostics))
