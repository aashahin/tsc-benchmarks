import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
RESULTS = ROOT / "results"


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    staging.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def append_json(path, document):
    with Path(path).open("a") as stream:
        stream.write(json.dumps(document, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_journal(path):
    if not Path(path).exists():
        return []
    records = []
    with Path(path).open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                raise ValueError(f"Incomplete journal record in {path}; preserve and repair before resuming")
            records.append(json.loads(line))
    return records


@contextlib.contextmanager
def exclusive_lock():
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / "suite.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another preparation or benchmark is already running") from None
        yield


def tree_digest(directory):
    hasher = hashlib.sha256()
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and not path.is_symlink():
            hasher.update(str(path.relative_to(directory)).encode() + b"\0")
            hasher.update(bytes.fromhex(digest(path)))
    return hasher.hexdigest()
