import dataclasses
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

from .compilers import bun_cache, compiler_env
from .resources import Command, GIB, measured_run, wait_for_capacity
from .storage import CACHE, ROOT, digest, read_json, tree_digest, write_json


def catalog():
    workloads = read_json(ROOT / "workloads.json")["workloads"]
    identifiers = [workload["id"] for workload in workloads]
    if len(set(identifiers)) != len(identifiers) or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in identifiers):
        raise ValueError("Workload IDs must be unique lowercase names without path separators")
    return workloads


def select_workloads(selection):
    workloads = catalog()
    groups = {"all": {"real-project", "stress", "workflow"}, "offline": {"stress", "workflow"},
              "smoke": {"smoke"}, "real": {"real-project"}, "stress": {"stress"}}
    requested = selection.split(",")
    known = {workload["id"] for workload in workloads} | set(groups)
    if set(requested) - known:
        raise ValueError(f"Unknown workload: {', '.join(sorted(set(requested) - known))}")
    return [workload for workload in workloads if workload["id"] in requested or any(
        workload["category"] in groups.get(name, set()) for name in requested)]


def jsonc(text):
    strings = r'("(?:\\.|[^"\\])*")'
    text = re.sub(strings + r'|/\*[\s\S]*?\*/|//[^\n]*', lambda match: match[1] or "", text)
    text = re.sub(strings + r'|,\s*([}\]])', lambda match: match[1] or match[2], text)
    return json.loads(text)


def verify_vendor(directory):
    source = read_json(directory / "SOURCE.json")
    for entry in source["files"]:
        contents = (directory / entry["path"]).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()
        if actual != entry["git_blob_sha1"]:
            raise RuntimeError(f"Upstream source hash mismatch: {entry['path']}")
    return source


def base_config():
    return {"compilerOptions": {"target": "es2022", "module": "preserve", "moduleResolution": "bundler",
            "strict": True, "types": [], "skipLibCheck": False, "noEmit": True}, "include": ["src/**/*.ts"]}


def write_source(folder, filename, source):
    path = folder / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def generate_edit_graph(folder, count):
    write_source(folder, "src/shared.ts", "export interface Shared { id: string; revision?: number }\n")
    write_source(folder, "src/leaf.ts", "export function leaf(): number { return 1; }\n")
    for index in range(count):
        write_source(folder, f"src/module-{index}.ts", f'''import type {{ Shared }} from "./shared";
type DeepReadonly<T> = {{ readonly [K in keyof T]: T[K] extends object ? DeepReadonly<T[K]> : T[K] }};
export type Entity{index} = DeepReadonly<Shared & {{ group: "g{index}"; rows: {{ value: number; tags: string[] }}[] }}>;
export function project{index}(input: Entity{index}): string {{ return input.id; }}
''')


def generate_smoke(folder, identifier):
    if identifier == "smoke-errors":
        write_source(folder, "src/index.ts", 'export const count: number = "wrong";\nexport const missing = unknownName;\n')
    elif identifier == "smoke-references":
        write_source(folder, "library/index.ts", 'export interface Shared { id: string }\n')
        write_source(folder, "src/index.ts", 'import type { Shared } from "../library/index";\nexport const shared: Shared = { id: "ok" };\n')
        write_json(folder / "library/tsconfig.json", {"compilerOptions": {"composite": True, "declaration": True,
                   "emitDeclarationOnly": True, "strict": True, "types": [], "outDir": "../built"}, "files": ["index.ts"]})
        configuration = base_config()
        configuration["references"] = [{"path": "./library"}]
        write_json(folder / "tsconfig.json", configuration)
    else:
        write_source(folder, "src/index.ts", 'export const count: number = 42;\n')


@dataclasses.dataclass
class Preparation:
    compilers: list
    limits: object
    offline: bool
    sequence: int = 0
    session: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)

    def environment(self):
        for directory in ("bun-install-cache", "bun-tmp"):
            (CACHE / directory).mkdir(parents=True, exist_ok=True)
        return {**compiler_env(len(self.limits.cpus)), "CI": "1", "GIT_TERMINAL_PROMPT": "0",
                "BUN_INSTALL_CACHE_DIR": str(CACHE / "bun-install-cache"), "BUN_TMPDIR": str(CACHE / "bun-tmp"),
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1", "PUPPETEER_SKIP_DOWNLOAD": "1",
                "ELECTRON_SKIP_BINARY_DOWNLOAD": "1"}

    def bun_binary(self):
        return next(compiler.path for compiler in self.compilers if compiler.name == "bun")

    def install_dependency(self, folder, name, version, bun=None):
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "package.json", {"private": True, "dependencies": {name: version}})
        command = [bun or self.bun_binary(), "install", "--ignore-scripts"]
        if any((folder / lock).exists() for lock in ("bun.lock", "bun.lockb")):
            command.append("--frozen-lockfile")
        self.run(command, folder)

    def run(self, argv, cwd):
        wait_for_capacity(self.limits)
        self.sequence += 1
        prefix = CACHE / "setup-logs" / f"{self.session}-{self.sequence}"
        command = Command([str(argument) for argument in argv], cwd, self.environment(), prefix)
        attempt = measured_run(command, dataclasses.replace(self.limits, timeout_seconds=900))
        write_json(prefix.with_suffix(".json"), attempt)
        if attempt["status"] == "interrupted":
            raise KeyboardInterrupt
        if attempt["status"] != "exited" or attempt["exit_code"]:
            raise RuntimeError(f"Setup failed ({attempt['status']}, exit {attempt['exit_code']}): {' '.join(command.argv)}; see {prefix}.stderr")
        return Path(attempt["stdout"]).read_text().strip()

    def checkout(self, workload, folder):
        marker = folder / ".bench-checkout.json"
        if marker.exists() and read_json(marker) != {"repository": workload["repository"], "commit": workload["commit"]}:
            raise RuntimeError(f"Checkout ownership/pin differs: {folder}")
        head = folder / ".git/HEAD"
        if head.exists() and re.fullmatch(r"[0-9a-f]{40}\s*", head.read_text()):
            revision = self.run(["git", "rev-parse", "HEAD"], folder)
            if revision != workload["commit"]:
                raise RuntimeError(f"Existing checkout differs from pin: {folder}")
            return
        if self.offline:
            raise RuntimeError(f"{workload['id']} is not cached; online preparation is required")
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / ".git").exists() and not marker.exists():
            raise RuntimeError(f"Refusing to reuse a checkout without the suite ownership marker: {folder}")
        write_json(marker, {"repository": workload["repository"], "commit": workload["commit"]})
        self.run(["git", "init", "-q"], folder)
        self.run(["git", "fetch", "--depth", "1", workload["repository"], workload["commit"]], folder)
        self.run(["git", "checkout", "--detach", "FETCH_HEAD"], folder)

    def install(self, folder):
        bun = self.bun_binary()
        locks = [folder / "bun.lock", folder / "bun.lockb"]
        if not any(path.exists() for path in locks):
            self.check_migration(folder)
            self.run([bun, "pm", "migrate"], folder)
        lock = next((path for path in locks if path.exists()), None)
        if lock is None:
            raise RuntimeError("Bun did not create a lockfile; dependency preparation is blocked")
        self.run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], folder)
        return {"package_manager": "bun", "lockfile": lock.name, "lockfile_sha256": digest(lock)}

    def check_migration(self, folder):
        npm_lock, yarn_lock, pnpm_lock = (folder / name for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
        if npm_lock.exists() and read_json(npm_lock).get("lockfileVersion", 1) < 2:
            raise RuntimeError("Bun cannot migrate npm lockfile v1 without resolving new versions; preparation is blocked")
        if yarn_lock.exists() and "# yarn lockfile v1" not in yarn_lock.read_text():
            raise RuntimeError("Bun requires a Yarn v1 lockfile for migration; preparation is blocked")
        if not any(path.exists() for path in (npm_lock, yarn_lock, pnpm_lock)):
            raise RuntimeError("No supported dependency lockfile found; preparation is blocked")

    def prepare_repository(self, workload, folder):
        self.checkout(workload, folder)
        marker = folder / ".bench-installed.json"
        installed = read_json(marker) if marker.exists() else {}
        if installed.get("package_manager") != "bun":
            if self.offline:
                raise RuntimeError("Dependencies have not been installed with Bun for this pinned checkout")
            installation = self.install(folder)
            write_json(marker, {"commit": workload["commit"], **installation})
        adapter = {"plain": self.plain_adapter, "vscode": self.vscode_adapter, "playwright": self.playwright_adapter,
                   "excalidraw": self.excalidraw_adapter, "typeorm": self.typeorm_adapter}[workload["adapter"]]
        return adapter(folder, folder / workload["config"])

    def plain_adapter(self, folder, config):
        return config

    def vscode_adapter(self, folder, config):
        electron = folder / "node_modules/electron"
        if not electron.exists():
            if self.offline:
                raise RuntimeError("VS Code requires the pinned Electron typings; prepare online")
            staging = CACHE / "toolchains/electron"
            self.install_dependency(staging, "electron", "43.7.7")
            shutil.copytree(staging / "node_modules/electron", electron)
        return config

    def playwright_adapter(self, folder, config):
        self.run([self.bun_binary(), "utils/generate_injected.js"], folder)
        return config

    def excalidraw_adapter(self, folder, config):
        configuration = jsonc(config.read_text())
        configuration["compilerOptions"].pop("baseUrl", None)
        target = config.with_name("tsconfig.bench.json")
        write_json(target, configuration)
        return target

    def typeorm_adapter(self, folder, config):
        target = config.with_name("tsconfig.bench.json")
        write_json(target, {"extends": "./tsconfig.json", "compilerOptions": {"module": "nodenext", "moduleResolution": "nodenext"}})
        return target

    def prepare_zod(self, workload, folder):
        verify_vendor(ROOT / "vendor/zod")
        version = read_json(ROOT / "workloads.json")["zod_version"]
        dependency = folder / "node_modules/zod"
        if not dependency.exists():
            cached = bun_cache("zod", version)
            if cached:
                dependency.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cached, dependency)
            elif self.offline:
                raise RuntimeError(f"Zod {version} is not cached")
            else:
                self.install_dependency(folder, "zod", version)
        if read_json(dependency / "package.json")["version"] != version:
            raise RuntimeError("Cached Zod version differs from the manifest")
        write_json(folder / "dependency-lock.json", {"name": "zod", "version": version, "tree_sha256": tree_digest(dependency)})
        self.run([self.bun_binary(), ROOT / "scripts/generate-zod.ts", json.dumps(workload)], folder)
        write_json(folder / "tsconfig.json", base_config())
        return folder / "tsconfig.json"

    def prepare_vendored(self, workload, folder):
        vendor = ROOT / "vendor" / workload["vendor"]
        verify_vendor(vendor)
        shutil.copytree(vendor, folder, dirs_exist_ok=True)
        target = folder / "tsconfig.bench.json"
        write_json(target, {"extends": "./tsconfig.json", "compilerOptions": {"strict": False}})
        return target

    def prepare_generated(self, workload, folder):
        write_json(folder / "tsconfig.json", base_config())
        if workload["id"] == "edit-graph":
            generate_edit_graph(folder, workload["modules"])
        else:
            generate_smoke(folder, workload["id"])
        if workload.get("references"):
            tsc = next(compiler.path for compiler in self.compilers if compiler.name == "tsc")
            self.run([tsc, "-b", "library/tsconfig.json", "--builders", "1"], folder)
        return folder / "tsconfig.json"

    def prepare(self, workload):
        folder = CACHE / "workloads" / workload["id"]
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < 10 * GIB:
            raise RuntimeError("Less than 10 GiB disk space remains; preparation deferred")
        if workload["kind"] in ("generated", "zod") and (folder / "src").exists():
            shutil.rmtree(folder / "src")
        factory = {"repository": self.prepare_repository, "vendored": self.prepare_vendored,
                   "zod": self.prepare_zod, "generated": self.prepare_generated}[workload["kind"]]
        config = factory(workload, folder)
        provenance = {}
        if workload["kind"] == "vendored":
            provenance = read_json(ROOT / "vendor" / workload["vendor"] / "SOURCE.json")
        elif workload["kind"] == "zod":
            provenance = {"generator": read_json(ROOT / "vendor/zod/SOURCE.json"),
                          "dependency": read_json(folder / "dependency-lock.json"),
                          "seed": 20261008, "driver_sha256": digest(ROOT / "scripts/generate-zod.ts")}
        return {**workload, "root": str(folder), "project": str(config), "config_sha256": digest(config),
                "recipe_sha256": digest(ROOT / "workloads.json"), "provenance": provenance}


def prepare_all(workloads, preparation):
    path = CACHE / "prepared.json"
    prepared = read_json(path) if path.exists() else {}
    failures = []
    for workload in workloads:
        print(f"Preparing {workload['id']}…", flush=True)
        try:
            prepared[workload["id"]] = preparation.prepare(workload)
        except (RuntimeError, OSError, ValueError) as error:
            prepared[workload["id"]] = {**workload, "error": str(error)}
            failures.append(workload["id"])
            print(f"  unavailable: {error}", flush=True)
        write_json(path, prepared)
    return failures
