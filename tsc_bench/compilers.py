import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .storage import CACHE, digest, read_json, write_json


def compiler_env(threads):
    environment = os.environ.copy()
    for name in ("AGENT", "CLAUDECODE", "REPL_ID", "FORCE_COLOR", "GODEBUG", "GOGC", "GOMEMLIMIT"):
        environment.pop(name, None)
    environment.update({"NO_COLOR": "1", "TERM": "dumb", "LC_ALL": "C", "GOMAXPROCS": str(threads)})
    return environment


def output(argv):
    return subprocess.check_output(argv, text=True, env=compiler_env(2), timeout=15).strip()


def bun_cache(package, version):
    parent = Path.home() / ".bun/install/cache"
    return next((path for path in sorted(parent.glob(f"{package}@{version}@*")) if path.is_dir()), None)


def native_tsc_candidate(version):
    candidates = [CACHE / "toolchains/typescript/lib/tsc",
                  CACHE / "toolchains/typescript-package/node_modules/@typescript/typescript-linux-x64/lib/tsc"]
    cached = bun_cache("@typescript/typescript-linux-x64", version)
    if cached:
        candidates.append(cached / "lib/tsc")
    return next((path for path in candidates if path.is_file()), None)


@dataclasses.dataclass(frozen=True)
class Compiler:
    name: str
    path: Path
    version: str
    sha256: str
    typescript_version: str

    def argv(self, project, threads, cache=None):
        shared = ["-p", str(project), "--noEmit", "--noCheck", "false", "--composite", "false"]
        if cache is None:
            shared += ["--incremental", "false"]
        elif self.name == "tsc":
            shared += ["--incremental", "true", "--tsBuildInfoFile", str(cache)]
        if self.name == "bun":
            return [str(self.path), "check", *shared, "--no-pretty", "--all", "--threads", str(threads)]
        concurrency = ["--singleThreaded"] if threads == 1 else ["--checkers", str(threads)]
        return [str(self.path), *shared, "--pretty", "false", *concurrency]

    def metadata(self):
        return {**dataclasses.asdict(self), "path": str(self.path)}


def inspect_compilers(bun_path=None, tsc_path=None):
    resolved_bun = bun_path or shutil.which("bun")
    if not resolved_bun:
        raise RuntimeError("Bun with bun check is required; supply --bun /path/to/bun")
    bun_path = Path(resolved_bun).resolve()
    versions = json.loads(output([str(bun_path), "-p", "JSON.stringify({revision:Bun.revision,typescript:process.versions.typescript})"]))
    if not versions.get("typescript"):
        raise RuntimeError("This Bun does not advertise a TypeScript checker")
    tsc_path = Path(tsc_path).resolve() if tsc_path else native_tsc_candidate(versions["typescript"])
    if tsc_path is None:
        raise RuntimeError(f"Native TypeScript {versions['typescript']} not found; run prepare online or supply --tsc")
    with tsc_path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError("--tsc must point to the native Linux binary, not the npm/Node launcher")
    tsc_version = output([str(tsc_path), "--version"]).removeprefix("Version ")
    if not re.match(r"(?:[7-9]|\d{2,})\.", tsc_version):
        raise RuntimeError("The comparison requires TypeScript 7 or newer")
    return [Compiler("bun", bun_path, output([str(bun_path), "--revision"]), digest(bun_path), versions["typescript"]),
            Compiler("tsc", tsc_path, tsc_version, digest(tsc_path), tsc_version)]


def pin_compilers(compilers):
    pinned = []
    for compiler in compilers:
        target = CACHE / "toolchains" / ("bun/bun" if compiler.name == "bun" else "typescript/lib/tsc")
        if target.exists() and digest(target) != compiler.sha256:
            raise RuntimeError(f"Pinned compiler differs at {target}; keep existing results and use explicit compiler paths")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if compiler.name == "tsc":
                shutil.copytree(compiler.path.parent, target.parent, dirs_exist_ok=True)
            else:
                shutil.copy2(compiler.path, target)
        pinned.append(dataclasses.replace(compiler, path=target))
    write_json(CACHE / "toolchains/lock.json", [compiler.metadata() for compiler in pinned])
    return pinned


def load_compilers(bun_path=None, tsc_path=None):
    lock = CACHE / "toolchains/lock.json"
    if bun_path or tsc_path or not lock.exists():
        return inspect_compilers(bun_path, tsc_path)
    compilers = [Compiler(**{**entry, "path": Path(entry["path"])}) for entry in read_json(lock)]
    for compiler in compilers:
        if digest(compiler.path) != compiler.sha256:
            raise RuntimeError(f"Compiler changed since preparation: {compiler.path}")
    return compilers
