"""Private Git workspaces and controller-created immutable candidates.

A private clone is deliberately used instead of a linked worktree: its writable
Git metadata cannot update another worker's refs.  This is logical isolation,
not an operating-system sandbox.  Candidate construction uses a second, trusted
bare repository, so worker Git hooks/configuration are not executed by capture.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from typing import Any

from .contracts import Fault


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Git environment variables can redirect the repository or load executable
    # filters.  Do not inherit them from an interactive shell or worker.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "xiaolvji",
                "GIT_AUTHOR_EMAIL": "runtime@localhost", "GIT_COMMITTER_NAME": "xiaolvji",
                "GIT_COMMITTER_EMAIL": "runtime@localhost", "LC_ALL": "C"})
    if extra:
        env.update(extra)
    return env


def _git_bytes(*args: str, cwd: str | Path | None = None,
               env: dict[str, str] | None = None) -> bytes:
    command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
               "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", *map(str, args)]
    try:
        result = subprocess.run(command, cwd=cwd, env=_environment(env),
                                capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Fault("WORKSPACE_ERROR", f"Git operation failed: {type(exc).__name__}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace")[-2000:].strip()
        raise Fault("WORKSPACE_ERROR", f"Git operation failed: {message}")
    return result.stdout


def git(*args: str, cwd: str | Path | None = None,
        env: dict[str, str] | None = None) -> str:
    """Run Git without a shell and return stripped UTF-8 text."""
    return _git_bytes(*args, cwd=cwd, env=env).decode("utf-8", "strict").strip()


def tree_digest(repository: str | Path, commit: str) -> str:
    """Digest the exact tracked paths, modes and object IDs of a Git tree."""
    if not _OID.fullmatch(commit):
        raise Fault("INVALID_ARTIFACT", "A full commit or tree object ID is required")
    listing = _git_bytes("--git-dir", str(repository), "ls-tree", "-r", "-z", commit)
    return "sha256:" + hashlib.sha256(listing).hexdigest()


# Public name used by evidence and integration components.
artifact_digest = tree_digest


def _is_below(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise Fault("SCOPE_CONFLICT", "Invalid relative path in candidate")
    rel = PurePosixPath(value)
    if rel.is_absolute() or ".." in rel.parts or any(p in {".git", ".parallel-dev"} for p in rel.parts):
        raise Fault("SCOPE_CONFLICT", "Candidate path leaves the permitted source tree")
    return rel


def _allowed(path: str, scopes: list[dict[str, Any]]) -> bool:
    rel = _safe_relative(path)
    for scope in scopes:
        allowed = PurePosixPath(scope["path"])
        if scope["kind"] == "file" and rel == allowed:
            return True
        if scope["kind"] == "directory" and (rel == allowed or allowed in rel.parents or str(allowed) == "."):
            return True
    return False


class Workspaces:
    def __init__(self, root: str | Path):
        original = Path(root).absolute()
        if original.is_symlink() or original.resolve() != original:
            raise Fault("WORKSPACE_ERROR", "Workspace root must not contain symlink aliases")
        original.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = original.resolve()

    def create(self, repository: str | Path, attempt_id: str, base_commit: str) -> dict[str, Any]:
        if not _ID.fullmatch(attempt_id) or not _OID.fullmatch(base_commit):
            raise Fault("INVALID_TASK", "Invalid attempt or baseline identity")
        repository = Path(repository).resolve(strict=True)
        if _is_below(self.root, repository):
            raise Fault("WORKSPACE_ERROR", "Managed workspaces must be outside the source repository")
        base = git("rev-parse", "--verify", "--end-of-options", base_commit + "^{commit}", cwd=repository)
        if base != base_commit:
            raise Fault("INVALID_TASK", "Baseline must identify a commit directly")
        gitlinks = _git_bytes("ls-tree", "-r", "-z", base, cwd=repository)
        if any(record.startswith(b"160000 ") for record in gitlinks.split(b"\0")):
            raise Fault("CAPABILITY_UNSUPPORTED", "Submodule workspaces are not supported in runtime 0.1")
        owner = self.root / attempt_id
        try:
            owner.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Fault("INSTANCE_UNCERTAIN", "Attempt workspace already exists; reconcile before reuse") from exc
        path, metadata = owner / "worktree", owner / "candidate.git"
        workspace = {"attempt_id": attempt_id, "path": str(path), "repository": str(repository),
                     "base_commit": base, "branch": f"refs/heads/parallel/{attempt_id}",
                     "candidate_repository": str(metadata), "owner": str(owner),
                     "index": str(owner / "candidate.index"), "schema_version": 1}
        # Ownership is created before clone, allowing explicit cleanup of failed
        # construction without guessing which directory belongs to this runtime.
        self._write(owner / "ownership.json", workspace)
        try:
            _git_bytes("clone", "--no-hardlinks", "--no-local", "--no-checkout", "--no-recurse-submodules",
                       "--", str(repository), str(path))
            # Candidate commits may only be reachable under refs/candidates,
            # which a normal clone's default refspec does not include.
            git("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
                str(repository), base, cwd=path)
            git("checkout", "--detach", base, cwd=path)
            git("switch", "-c", f"parallel/{attempt_id}", cwd=path)
            _git_bytes("clone", "--bare", "--no-hardlinks", "--no-local", "--", str(path), str(metadata))
            # Worker-created commits are not trusted as candidates. This repo's
            # index and refs are only modified by controller capture.
            self._git(workspace, "read-tree", base)
            self._write(owner / "baseline-files.json", self._files(path))
            return workspace
        except BaseException:
            # Keep the ownership journal for explicit cleanup/reconciliation.
            raise

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

    def _owned(self, workspace: dict[str, Any]) -> dict[str, Any]:
        attempt_id = workspace.get("attempt_id", "")
        if not _ID.fullmatch(attempt_id):
            raise Fault("WORKSPACE_ERROR", "Invalid owned workspace identity")
        owner = self.root / attempt_id
        marker = owner / "ownership.json"
        if owner.is_symlink() or marker.is_symlink() or not marker.is_file():
            raise Fault("WORKSPACE_ERROR", "Workspace ownership could not be verified")
        try:
            stored = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise Fault("WORKSPACE_ERROR", "Invalid workspace ownership journal") from exc
        if stored != workspace or stored.get("owner") != str(owner):
            raise Fault("WORKSPACE_ERROR", "Workspace does not match its ownership journal")
        for key in ("path", "candidate_repository"):
            expected = owner / ("worktree" if key == "path" else "candidate.git")
            if Path(stored[key]) != expected or expected.is_symlink():
                raise Fault("WORKSPACE_ERROR", "Owned workspace path has changed")
        if Path(stored["index"]).is_symlink():
            raise Fault("WORKSPACE_ERROR", "Candidate index must not be a symlink")
        return stored

    @staticmethod
    def _git(workspace: dict[str, Any], *args: str) -> bytes:
        return _git_bytes("--git-dir", workspace["candidate_repository"], "--work-tree", workspace["path"],
                          *args, cwd=workspace["path"], env={"GIT_INDEX_FILE": workspace["index"]})

    @staticmethod
    def _files(root: Path) -> dict[str, dict[str, Any]]:
        """Hash source files, including ignored files; never follow symlinks."""
        result = {}
        for folder, directories, files in os.walk(root, followlinks=False):
            parent = Path(folder)
            if parent == root:
                directories[:] = [name for name in directories if name != ".git"]
                files = [name for name in files if name != ".git"]
            for name in list(directories):
                if name == ".git":
                    raise Fault("CAPABILITY_UNSUPPORTED", "Nested Git repositories are not supported")
                target = parent / name
                if target.is_symlink():
                    directories.remove(name)
                    files.append(name)
            for name in files:
                target = parent / name
                relative = target.relative_to(root).as_posix()
                info = target.lstat()
                mode = stat.S_IFMT(info.st_mode)
                if stat.S_ISLNK(mode):
                    result[relative] = {"kind": "symlink", "target": os.readlink(target)}
                elif stat.S_ISREG(mode):
                    hasher = hashlib.sha256()
                    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as source:
                        while chunk := source.read(1024 * 1024):
                            hasher.update(chunk)
                    result[relative] = {"kind": "file", "digest": hasher.hexdigest(),
                                        "executable": bool(info.st_mode & 0o111)}
                else:
                    raise Fault("SCOPE_CONFLICT", f"Workspace contains a special file: {relative}")
        return result

    def snapshot(self, workspace: dict[str, Any], scopes: list[dict[str, Any]]) -> dict[str, Any]:
        workspace = self._owned(workspace)
        path = Path(workspace["path"])
        if not (path / ".git").is_dir() or (path / ".git").is_symlink():
            raise Fault("WORKSPACE_ERROR", "Worker Git metadata was replaced")
        baseline_path = Path(workspace["owner"]) / "baseline-files.json"
        if baseline_path.is_symlink():
            raise Fault("WORKSPACE_ERROR", "Baseline journal unexpectedly became a symlink")
        try:
            baseline_files = json.loads(baseline_path.read_text())
        except (OSError, ValueError) as exc:
            raise Fault("WORKSPACE_ERROR", "Baseline file journal is unavailable") from exc
        actual_files = self._files(path)
        filesystem_changes = sorted(name for name in baseline_files.keys() | actual_files.keys()
                                    if baseline_files.get(name) != actual_files.get(name))
        for name in filesystem_changes:
            if not _allowed(name, scopes):
                raise Fault("SCOPE_CONFLICT", f"Workspace changed an unleased path: {name}")
            if actual_files.get(name, {}).get("kind") == "symlink":
                raise Fault("SCOPE_CONFLICT", f"Workspace contains a changed symlink: {name}")
        # Reset the private index each time; a prior rejected capture cannot leak
        # staged content into a later candidate.
        self._git(workspace, "read-tree", workspace["base_commit"])
        self._git(workspace, "add", "-A", "--", ".")
        diff = self._git(workspace, "diff", "--cached", "--name-only", "--no-renames", "-z", workspace["base_commit"], "--")
        try:
            changed = sorted(x.decode("utf-8", "strict") for x in diff.split(b"\0") if x)
        except UnicodeError as exc:
            raise Fault("CAPABILITY_UNSUPPORTED", "Non-UTF-8 filenames are not supported") from exc
        for name in changed:
            if not _allowed(name, scopes):
                raise Fault("SCOPE_CONFLICT", f"Candidate changed an unleased path: {name}")
            current = path
            for part in _safe_relative(name).parts:
                current = current / part
                if current.is_symlink():
                    raise Fault("SCOPE_CONFLICT", f"Candidate contains a changed symlink: {name}")
                if current.exists() and not _is_below(current.resolve(), path):
                    raise Fault("SCOPE_CONFLICT", f"Candidate path escaped the workspace: {name}")
        tree = self._git(workspace, "write-tree").decode().strip()
        listing = self._git(workspace, "ls-tree", "-r", "-z", tree)
        if any(record.startswith(b"160000 ") for record in listing.split(b"\0")):
            raise Fault("CAPABILITY_UNSUPPORTED", "Nested Git repositories and submodules are not supported")
        commit = self._git(workspace, "commit-tree", tree, "-p", workspace["base_commit"],
                           "-m", f"parallel candidate {workspace['attempt_id']}").decode().strip()
        candidate_ref = f"refs/candidates/{workspace['attempt_id']}"
        self._git(workspace, "update-ref", candidate_ref, commit)
        return {"commit": commit, "tree": tree, "artifact_digest": "sha256:" + hashlib.sha256(listing).hexdigest(),
                "changed_paths": changed, "workspace_changed_paths": filesystem_changes,
                "base_commit": workspace["base_commit"],
                "repository": workspace["candidate_repository"], "branch": candidate_ref}

    def remove(self, workspace: dict[str, Any]) -> None:
        """Remove an explicitly owned workspace; callers must prove no live run."""
        workspace = self._owned(workspace)
        # shutil.rmtree on this platform has symlink-attack resistance. Refuse
        # unsupported platforms rather than traversing an attacker-swapped tree.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise Fault("CAPABILITY_UNSUPPORTED", "Safe recursive workspace removal is unavailable")
        shutil.rmtree(workspace["owner"])
