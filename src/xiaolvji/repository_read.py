"""Bounded reads of reachable committed project data, never worktree contents.

The caller supplies a transport-authenticated principal. A tool name, a hook
response, or a caller-provided role cannot grant this authority. No Git command
here checks out files, executes filters, follows submodules, or fetches objects.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable

from .contracts import Fault, Principal, validate_identifier


READ_ROLES = frozenset({"operator", "planner", "coordinator", "executor", "verifier", "integrator", "reviewer", "reader"})
MAX_READ_BYTES = 131_072
MAX_PATH_BYTES = 4096
MAX_TREE_PAGE = 200
MAX_TREE_CURSOR = 1_000_000
MAX_TREE_SCAN_BYTES = 64 * 1024 * 1024
MAX_PAGE_PATH_BYTES = 65_536
REQUEST_TIMEOUT = 10.0
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_RECORD = re.compile(rb"(040000|100644|100755|120000|160000) (tree|blob|commit) ([0-9a-f]{40}|[0-9a-f]{64})\t(.+)\Z", re.DOTALL)


def _relative(value: object, *, directory: bool = False) -> str:
    if directory and value == ".":
        return "."
    if not isinstance(value, str) or not value:
        raise Fault("INVALID_INPUT", "A nonempty relative Git path is required.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise Fault("INVALID_INPUT", "Git paths must be valid UTF-8.") from exc
    if (size > MAX_PATH_BYTES or value.startswith("/") or "\\" in value
            or re.match(r"^[A-Za-z]:", value) or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", "..", ".git", ".parallel-dev"} for part in value.split("/"))):
        raise Fault("INVALID_INPUT", "Git path is absolute, ambiguous, reserved, or outside the readable source tree.")
    return value


def _oid(value: object) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise Fault("INVALID_INPUT", "An exact full lowercase Git commit ID is required.")
    return value


def _integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Fault("INVALID_INPUT", label + " is outside the supported integer bounds.")
    return value


def _project(ledger, principal: Principal, project_id: str) -> dict:
    if not isinstance(principal, Principal) or not principal.id or principal.role not in READ_ROLES:
        raise Fault("UNAUTHORIZED", "Repository reads require a pre-authenticated controller reader.")
    validate_identifier(project_id, "project_id")
    project = ledger.project(project_id)
    if project.get("enabled") is not True:
        raise Fault("PROJECT_DISABLED", "Repository reads require an enabled project.")
    if not isinstance(project.get("plan"), dict):
        raise Fault("DEPENDENCY_BLOCKED", "Repository reads require an already approved project plan.")
    return project


def _binding(project: dict) -> dict:
    return {key: project["plan"][key] for key in ("id", "revision", "scope_digest")}


class _PageComplete(Exception):
    """The next record proves a continuation; stop Git instead of buffering it."""


class _GitRead:
    def __init__(self, root: str):
        path = Path(root)
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
                raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled repository root changed or is unavailable.")
            root_info = path.lstat()
            marker_info = (path / ".git").lstat()
            if not stat.S_ISDIR(root_info.st_mode) or not (stat.S_ISDIR(marker_info.st_mode) or stat.S_ISREG(marker_info.st_mode)):
                raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled Git root and metadata marker must not use aliases.")
        except (OSError, RuntimeError) as exc:
            raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled repository root is unavailable.") from exc
        git = shutil.which("git")
        if not git:
            raise Fault("CAPABILITY_UNSUPPORTED", "Git is required for committed repository reads.")
        self.root, self.git = path, str(Path(git).resolve())
        self.root_identity = self._directory_identity(root_info)
        self.marker_identity = self._marker_identity(marker_info)
        self.root_fd = None
        self.process_options = {"cwd": str(path)}
        if os.name == "posix" and Path("/proc/self/fd").is_dir():
            try:
                self.root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                if self._directory_identity(os.fstat(self.root_fd)) != self.root_identity:
                    raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled repository changed while it was opened.")
                # chdir through the held descriptor, so renaming/replacing the
                # enrolled path cannot redirect Git into another directory.
                self.process_options = {"cwd": f"/proc/self/fd/{self.root_fd}", "pass_fds": (self.root_fd,)}
                self._verify_root()
            except BaseException:
                if self.root_fd is not None:
                    os.close(self.root_fd)
                    self.root_fd = None
                raise
        self.deadline = time.monotonic() + REQUEST_TIMEOUT
        self.home = tempfile.TemporaryDirectory(prefix="xiaolvji-repository-read-")
        self.env = {
            "PATH": os.defpath, "HOME": self.home.name, "TMPDIR": self.home.name,
            "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0",
        }

    @staticmethod
    def _directory_identity(info):
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

    @classmethod
    def _marker_identity(cls, info):
        identity = cls._directory_identity(info)
        return identity if stat.S_ISDIR(info.st_mode) else identity + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _verify_root(self):
        try:
            if (self.root.resolve(strict=True) != self.root
                    or self._directory_identity(self.root.lstat()) != self.root_identity
                    or self._marker_identity((self.root / ".git").lstat()) != self.marker_identity):
                raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled repository or its Git marker changed during the read.")
        except (OSError, RuntimeError) as exc:
            raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled repository changed or became unavailable.") from exc

    def _close(self):
        self.home.cleanup()
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def __enter__(self):
        try:
            # Prevent Git's parent-directory discovery from widening enrollment.
            _, output = self.run(["rev-parse", "--show-toplevel"], MAX_PATH_BYTES + 1)
            top = Path(output.decode("utf-8").rstrip("\n"))
            if top.resolve(strict=True) != self.root:
                raise Fault("REPOSITORY_UNAVAILABLE", "The enrolled path must be the Git working-tree root.")
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, *exc):
        self._close()

    def run(self, args: list[str], limit: int, *, accept: tuple[int, ...] = (0,),
            consume: Callable[[bytes], None] | None = None) -> tuple[int, bytes]:
        """Drain bounded chunks on both pipes, with one deadline for the tool.

        Queue backpressure bounds captured stdout independently of repository
        size. Tree pagination deliberately terminates listing after one extra
        record; a full read still requires normal Git exit and exact framing.
        """
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Fault("REPOSITORY_TIMEOUT", "Committed repository read exceeded its deadline.")
        self._verify_root()
        command = [self.git, "--no-pager", "--no-replace-objects", "--literal-pathspecs",
                   "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                   "-c", "credential.helper=", "-c", "protocol.allow=never",
                   "-c", "gc.auto=0", *args]
        try:
            process = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **self.process_options)
        except OSError as exc:
            raise Fault("REPOSITORY_UNAVAILABLE", "Git could not start the committed read.") from exc
        chunks: queue.Queue = queue.Queue(maxsize=4)
        stop = threading.Event()

        def pump(pipe, label):
            try:
                while not stop.is_set():
                    block = pipe.read1(16_384)
                    item = (label, block)
                    while not stop.is_set():
                        try:
                            chunks.put(item, timeout=0.05)
                            break
                        except queue.Full:
                            pass
                    if not block:
                        return
            except (OSError, ValueError):
                while not stop.is_set():
                    try:
                        chunks.put(("error", b""), timeout=0.05)
                        break
                    except queue.Full:
                        pass

        threads = [threading.Thread(target=pump, args=(pipe, label), daemon=True)
                   for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
        for thread in threads:
            thread.start()
        output = bytearray()
        seen = {"stdout": 0, "stderr": 0}
        finished = set()
        try:
            while len(finished) < 2:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise Fault("REPOSITORY_TIMEOUT", "Committed repository read exceeded its deadline.")
                try:
                    label, block = chunks.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if label == "error":
                    raise Fault("REPOSITORY_UNAVAILABLE", "Git output could not be read safely.")
                if not block:
                    finished.add(label)
                    continue
                seen[label] += len(block)
                if seen[label] > (limit if label == "stdout" else 8192):
                    raise Fault("RESULT_TOO_LARGE", "Git read exceeded the bounded output budget.")
                if label == "stdout":
                    if consume is None:
                        output.extend(block)
                    else:
                        consume(block)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise Fault("REPOSITORY_TIMEOUT", "Committed repository read exceeded its deadline.")
            code = process.wait(timeout=remaining)
            if code not in accept:
                raise Fault("REPOSITORY_UNAVAILABLE", "The requested committed Git data is unavailable locally.")
            return code, bytes(output)
        except _PageComplete:
            return 0, b""
        except subprocess.TimeoutExpired as exc:
            raise Fault("REPOSITORY_TIMEOUT", "Committed repository read exceeded its deadline.") from exc
        finally:
            stop.set()
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise Fault("REPOSITORY_TIMEOUT", "Git termination could not be confirmed within the cleanup budget.") from exc
            finally:
                for thread in threads:
                    thread.join(timeout=0.1)
                # BufferedReader.close can wait on a live reader's lock. Only
                # close pipes after their owned reader has stopped; a kernel
                # stalled child must not turn this bounded read into a hang.
                for pipe, thread in zip((process.stdout, process.stderr), threads):
                    if not thread.is_alive():
                        pipe.close()
            self._verify_root()

    def snapshot(self, commit: str | None) -> str:
        _, raw = self.run(["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], 128)
        head = _oid(raw.decode("ascii").strip())
        selected = head if commit is None else _oid(commit)
        _, kind = self.run(["cat-file", "-t", selected], 32)
        if kind != b"commit\n":
            raise Fault("INVALID_INPUT", "The snapshot must identify a commit directly.")
        code, _ = self.run(["merge-base", "--is-ancestor", selected, head], 32, accept=(0, 1))
        if code != 0:
            raise Fault("SCOPE_CONFLICT", "The requested commit is not reachable from the enrolled project's current HEAD.")
        return selected

    def lookup(self, commit: str, path: str) -> dict:
        _, data = self.run(["ls-tree", "-z", "--full-tree", commit, "--", path], MAX_PATH_BYTES + 256)
        records = data.split(b"\0")
        if len(records) != 2 or records[-1] != b"" or not records[0]:
            raise Fault("NOT_FOUND", "The exact tracked path is absent from this commit.")
        entry = _entry(records[0], "")
        if entry["path"] != path:
            raise Fault("NOT_FOUND", "The exact tracked path is absent from this commit.")
        return entry


def _entry(record: bytes, prefix: str) -> dict:
    if len(record) > MAX_PATH_BYTES + 128:
        raise Fault("RESULT_TOO_LARGE", "A committed tree entry exceeds the path limit.")
    match = _RECORD.fullmatch(record)
    if match is None:
        raise Fault("REPOSITORY_UNAVAILABLE", "Git returned an unsupported tree entry.")
    mode, kind, object_id, raw_path = match.groups()
    try:
        path = raw_path.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise Fault("UNSUPPORTED_FILE", "Committed paths must be valid UTF-8.") from exc
    path = _relative(prefix + path)
    expected_kind = b"tree" if mode == b"040000" else b"commit" if mode == b"160000" else b"blob"
    if kind != expected_kind:
        raise Fault("REPOSITORY_UNAVAILABLE", "Git tree mode and object type disagree.")
    return {"path": path, "mode": mode.decode("ascii"), "type": kind.decode("ascii"),
            "object_id": object_id.decode("ascii")}


def project_tree(ledger, principal: Principal, project_id: str, *, directory: str = ".",
                 commit: str | None = None, cursor: int = 0, limit: int = 100) -> dict:
    project = _project(ledger, principal, project_id)
    directory = _relative(directory, directory=True)
    cursor = _integer(cursor, 0, MAX_TREE_CURSOR, "Tree cursor")
    limit = _integer(limit, 1, MAX_TREE_PAGE, "Tree page limit")
    if cursor and commit is None:
        raise Fault("INVALID_INPUT", "Continuation pages require the exact commit from the first page.")
    if commit is not None:
        _oid(commit)
    entries: list[dict] = []
    pending = bytearray()
    count = 0
    path_bytes = 0
    more = False
    with _GitRead(project["root"]) as reader:
        selected = reader.snapshot(commit)
        tree = selected
        if directory != ".":
            entry = reader.lookup(selected, directory)
            if entry["type"] != "tree":
                raise Fault("UNSUPPORTED_FILE", "The requested directory is not a committed Git tree.")
            tree = entry["object_id"]
        prefix = "" if directory == "." else directory + "/"

        def consume(block: bytes):
            nonlocal count, path_bytes, more
            pending.extend(block)
            while True:
                end = pending.find(b"\0")
                if end < 0:
                    if len(pending) > MAX_PATH_BYTES + 128:
                        raise Fault("RESULT_TOO_LARGE", "A committed tree entry exceeds the path limit.")
                    return
                record = bytes(pending[:end])
                del pending[:end + 1]
                entry = _entry(record, prefix)
                count += 1
                if count <= cursor:
                    continue
                length = len(entry["path"].encode("utf-8"))
                if len(entries) >= limit or path_bytes + length > MAX_PAGE_PATH_BYTES:
                    more = True
                    raise _PageComplete()
                entries.append(entry)
                path_bytes += length

        reader.run(["ls-tree", "-z", tree], MAX_TREE_SCAN_BYTES, consume=consume)
        if not more and pending:
            raise Fault("REPOSITORY_UNAVAILABLE", "Git tree output ended with an incomplete record.")
    # Re-check the explicit enrollment/plan after I/O; disable/revision changes
    # during a read cannot publish stale controller authority in the response.
    current = _project(ledger, principal, project_id)
    if _binding(current) != _binding(project) or current["root"] != project["root"]:
        raise Fault("CONTRACT_STALE", "The project plan changed during the committed read.")
    return {"project_id": project_id, "plan": _binding(project), "commit": selected,
            "directory": directory, "entries": entries,
            "next_cursor": cursor + len(entries) if more else None, "source": "committed_git"}


def read_file(ledger, principal: Principal, project_id: str, *, commit: str,
              path: str, max_bytes: int = 65_536) -> dict:
    project = _project(ledger, principal, project_id)
    commit, path = _oid(commit), _relative(path)
    max_bytes = _integer(max_bytes, 1, MAX_READ_BYTES, "File byte limit")
    with _GitRead(project["root"]) as reader:
        selected = reader.snapshot(commit)
        entry = reader.lookup(selected, path)
        if entry["mode"] not in {"100644", "100755"} or entry["type"] != "blob":
            raise Fault("UNSUPPORTED_FILE", "Only tracked regular files may be read; symlinks, directories, and gitlinks are rejected.")
        _, raw_size = reader.run(["cat-file", "-s", entry["object_id"]], 32)
        if not re.fullmatch(rb"[0-9]+\n", raw_size):
            raise Fault("REPOSITORY_UNAVAILABLE", "Git returned an invalid blob size.")
        size = int(raw_size)
        if size > max_bytes:
            raise Fault("RESULT_TOO_LARGE", "Committed file exceeds the requested byte limit.")
        _, content = reader.run(["cat-file", "blob", entry["object_id"]], max_bytes)
        if len(content) != size:
            raise Fault("REPOSITORY_UNAVAILABLE", "Committed blob size changed during the read.")
        algorithm = hashlib.sha1 if len(entry["object_id"]) == 40 else hashlib.sha256
        actual = algorithm(b"blob " + str(size).encode("ascii") + b"\0" + content).hexdigest()
        if actual != entry["object_id"]:
            raise Fault("REPOSITORY_UNAVAILABLE", "Committed blob content does not match its object ID.")
        try:
            text = content.decode("utf-8", "strict")
        except UnicodeError as exc:
            raise Fault("UNSUPPORTED_FILE", "Committed file is binary or is not valid UTF-8.") from exc
        if "\0" in text:
            raise Fault("UNSUPPORTED_FILE", "Committed file contains binary NUL bytes.")
    current = _project(ledger, principal, project_id)
    if _binding(current) != _binding(project) or current["root"] != project["root"]:
        raise Fault("CONTRACT_STALE", "The project plan changed during the committed read.")
    return {"project_id": project_id, "plan": _binding(project), "commit": selected,
            **entry, "bytes": size, "encoding": "utf-8", "content": text, "source": "committed_git"}
