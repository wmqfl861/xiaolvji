"""Immutable Git composition and compare-and-swap integration publication.

Composition never uses the user's checkout or index. Publication only moves a
dedicated, unchecked-out integration ref after the caller's combined checks;
the caller must hold the ledger's integration resource lease throughout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from typing import Any

from .contracts import Fault
from .workspace import _environment, artifact_digest, git


_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PREFIX = "refs/heads/parallel-dev/"


def _run(*args: str, cwd: str | Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                               "-c", "commit.gpgSign=false", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                               *map(str, args)], cwd=cwd, env=_environment(),
                              input=stdin, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Fault("INTEGRATION_ERROR", f"Git integration failed: {type(exc).__name__}") from exc


def _command(*args: str, cwd: str | Path, stdin: str | None = None) -> str:
    result = _run(*args, cwd=cwd, stdin=stdin)
    if result.returncode:
        raise Fault("INTEGRATION_ERROR", "Git integration operation failed", {"diagnostic": result.stderr[-2000:]})
    return result.stdout.strip()


def _commit(repository: str | Path, value: str) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise Fault("INVALID_ARTIFACT", "Integration requires full commit IDs")
    actual = _command("rev-parse", "--verify", "--end-of-options", value + "^{commit}", cwd=repository)
    if actual != value:
        raise Fault("INVALID_ARTIFACT", "Integration baseline is not a commit")
    return value


def _ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.startswith(_PREFIX) or len(ref) > 256:
        raise Fault("INVALID_ARGUMENT", "Integration may update only refs/heads/parallel-dev/*")
    # check-ref-format does not access a repository or load worker Git config.
    result = subprocess.run(["git", "check-ref-format", ref], capture_output=True, env=_environment())
    if result.returncode:
        raise Fault("INVALID_ARGUMENT", "Invalid integration reference")
    return ref


def _current(repository: str | Path, ref: str) -> str | None:
    # show-ref --verify returns 1 for a missing ref and avoids interpreting a
    # branch name as a revision expression.
    result = _run("show-ref", "--verify", "--hash", ref, cwd=repository)
    if result.returncode == 0:
        return result.stdout.strip()
    # Git versions return 1 or 128 for --verify of an absent ref. Determine the
    # absence from an exact for-each-ref lookup rather than parsing stderr.
    names = _command("for-each-ref", "--format=%(refname) %(objectname)", ref, cwd=repository)
    for line in names.splitlines():
        name, oid = line.split(" ", 1)
        if name == ref:
            return oid
    return None


def integration_baseline(repository: str | Path, ref: str = "refs/heads/parallel-dev/integration") -> dict[str, Any]:
    ref = _ref(ref)
    repository = Path(repository).resolve(strict=True)
    expected = _current(repository, ref)
    baseline = expected or _command("rev-parse", "--verify", "HEAD^{commit}", cwd=repository)
    return {"ref": ref, "expected_commit": expected, "baseline_commit": _commit(repository, baseline)}


class Composer:
    def __init__(self, state_dir: str | Path):
        path = Path(state_dir).absolute()
        if path.is_symlink() or path.resolve() != path:
            raise Fault("INVALID_ARGUMENT", "Composition state cannot contain symlink aliases")
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root = path

    def compose(self, repository: str | Path, baseline_commit: str,
                candidates: list[dict[str, Any]]) -> dict[str, Any]:
        repository = Path(repository).resolve(strict=True)
        if self.root == repository or repository in self.root.parents:
            raise Fault("INVALID_ARGUMENT", "Composition state must be outside the source repository")
        baseline_commit = _commit(repository, baseline_commit)
        if not isinstance(candidates, list) or len(candidates) > 256:
            raise Fault("INVALID_ARGUMENT", "Composition accepts at most 256 immutable candidates")
        composition_id = uuid.uuid4().hex
        owner = self.root / composition_id
        owner.mkdir(mode=0o700)
        combined_repo = owner / "combined.git"
        _command("clone", "--bare", "--no-hardlinks", "--no-local", "--", str(repository), str(combined_repo), cwd=owner)
        _command("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
                 str(repository), baseline_commit, cwd=combined_repo)
        result: dict[str, Any] = {"repository": str(combined_repo), "baseline_commit": baseline_commit,
                                  "composition_id": composition_id, "candidates": []}
        head = baseline_commit
        try:
            for index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    raise Fault("INVALID_ARTIFACT", "Candidate must be an immutable artifact record")
                try:
                    source = Path(candidate["repository"]).resolve(strict=True)
                    commit = _commit(source, candidate["commit"])
                    expected = candidate["artifact_digest"]
                except (KeyError, OSError, TypeError) as exc:
                    raise Fault("INVALID_ARTIFACT", "Candidate source or binding is missing") from exc
                if artifact_digest(source, commit) != expected:
                    raise Fault("ARTIFACT_CHANGED", "Candidate content no longer matches its approved digest")
                _command("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
                         str(source), f"{commit}:refs/candidates/{index}", cwd=combined_repo)
                if artifact_digest(combined_repo, commit) != expected:
                    raise Fault("ARTIFACT_CHANGED", "Fetched candidate does not match its original digest")
                # merge-tree uses the same merge machinery without checking out
                # files. Fresh clone config has no custom worker merge drivers.
                merged = _run("merge-tree", "--write-tree", "--name-only", head, commit, cwd=combined_repo)
                if merged.returncode == 129:
                    raise Fault("CAPABILITY_UNSUPPORTED", "Git merge-tree --write-tree support is required")
                if merged.returncode:
                    raise Fault("INTEGRATION_CONFLICT", "Candidate combination requires explicit conflict resolution",
                                {"candidate_index": index, "repository": str(combined_repo),
                                 "diagnostic": (merged.stdout + merged.stderr)[-4000:]})
                tree = merged.stdout.splitlines()[0].strip()
                if not _OID.fullmatch(tree):
                    raise Fault("INTEGRATION_ERROR", "Git returned an invalid combined tree")
                parents = ["-p", head]
                if head != commit:
                    parents.extend(["-p", commit])
                head = _command("commit-tree", tree, *parents, "-m", f"parallel integration {composition_id}", cwd=combined_repo)
                result["candidates"].append(json.loads(json.dumps(candidate)))
            _command("update-ref", "refs/heads/combined", head, cwd=combined_repo)
            result.update({"commit": head, "artifact_digest": artifact_digest(combined_repo, head)})
            (owner / "composition.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
            return result
        except Fault as exc:
            (owner / "failure.json").write_text(json.dumps({"code": exc.code, "message": exc.message,
                                "details": exc.details, "baseline_commit": baseline_commit}, sort_keys=True, indent=2) + "\n")
            raise

    @staticmethod
    def integration_baseline(repository: str | Path, ref: str = "refs/heads/parallel-dev/integration") -> dict[str, Any]:
        return integration_baseline(repository, ref)

    @staticmethod
    def publish(repository: str | Path, ref: str, expected_commit: str | None,
                candidate: dict[str, Any]) -> dict[str, Any]:
        return publish(repository, ref, expected_commit, candidate)


def publish(repository: str | Path, ref: str, expected_commit: str | None,
            candidate: dict[str, Any]) -> dict[str, Any]:
    """CAS an integration ref; caller must already verify this exact candidate."""
    ref = _ref(ref)
    repository = Path(repository).resolve(strict=True)
    if expected_commit is not None and not _OID.fullmatch(expected_commit):
        raise Fault("INVALID_ARGUMENT", "Expected integration ref must be a full commit ID")
    baseline = candidate.get("baseline_commit")
    if not isinstance(baseline, str) or not _OID.fullmatch(baseline):
        raise Fault("INVALID_ARTIFACT", "Combined candidate must bind its exact integration baseline")
    if expected_commit is not None and baseline != expected_commit:
        raise Fault("REVISION_CONFLICT", "Candidate was verified against a different integration baseline")
    if _current(repository, ref) != expected_commit:
        raise Fault("REVISION_CONFLICT", "Integration baseline changed; recombine and reverify")
    worktrees = _command("worktree", "list", "--porcelain", cwd=repository)
    if "branch " + ref in worktrees.splitlines():
        raise Fault("INTEGRATION_REF_CHECKED_OUT", "Ref is checked out in a worktree; publication would alter its baseline")
    try:
        source = Path(candidate["repository"]).resolve(strict=True)
        commit = _commit(source, candidate["commit"])
        expected_digest = candidate["artifact_digest"]
    except (KeyError, TypeError, OSError) as exc:
        raise Fault("INVALID_ARTIFACT", "Combined candidate binding is incomplete") from exc
    if artifact_digest(source, commit) != expected_digest:
        raise Fault("ARTIFACT_CHANGED", "Combined candidate differs from the verified artifact")
    _command("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
             str(source), commit, cwd=repository)
    git_directory = _command("rev-parse", "--absolute-git-dir", cwd=repository)
    if artifact_digest(git_directory, commit) != expected_digest:
        raise Fault("ARTIFACT_CHANGED", "Fetched combined candidate differs from the verified artifact")
    if expected_commit is None:
        # On initial publication the integration ref has no value. Verify HEAD
        # and create the new ref in one Git reference transaction.
        transaction = f"start\nverify HEAD {baseline}\ncreate {ref} {commit}\nprepare\ncommit\n"
        changed = _run("update-ref", "--stdin", cwd=repository, stdin=transaction)
    else:
        changed = _run("update-ref", ref, commit, expected_commit, cwd=repository)
    if changed.returncode:
        raise Fault("REVISION_CONFLICT", "Integration reference changed before atomic publication",
                    {"diagnostic": changed.stderr[-2000:]})
    return {"published": True, "ref": ref, "previous_commit": expected_commit,
            "commit": commit, "artifact_digest": expected_digest, "baseline_commit": baseline}
