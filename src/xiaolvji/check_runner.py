"""Run exact-artifact checks inside one admitted verifier/integrator process.

The outer execution gateway owns the ledger reservation. Each command uses the
same Linux process supervisor as an ordinary child of that job; no model worker
or additional ledger reservation is created. Source mutation and an unknown
process-tree exit both prevent technical acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from .contracts import Fault, canonical_json, digest, read_json, utc_now, validate_digest, validate_identifier
from .executor import ProcessExecutor

SCRATCH_NAME = ".parallel-dev-check-tmp"
MAX_CHECKS = 128
MAX_SOURCE_FILES = 100_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
COMMIT_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


def _absolute(value: Any, label: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Fault("INVALID_CHECK_MANIFEST", f"{label} must be an absolute path.")
    result = Path(value)
    if not result.is_absolute() or ".." in result.parts:
        raise Fault("INVALID_CHECK_MANIFEST", f"{label} must be absolute without traversal.")
    for component in (*reversed(result.parents), result):
        if component.is_symlink():
            raise Fault("INVALID_CHECK_MANIFEST", f"{label} cannot contain a symlink component.")
    if directory and not result.is_dir():
        raise Fault("INVALID_CHECK_MANIFEST", f"{label} must be an existing directory.")
    return result


def _output_path(path: Path, workspace: Path) -> None:
    if path.is_relative_to(workspace) and not path.is_relative_to(workspace / SCRATCH_NAME):
        raise Fault("INVALID_CHECK_MANIFEST", "Check reports and logs must be outside source or in the reserved check scratch directory.")


def validate_manifest(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"workspace", "checks", "artifact", "result_path", "log_dir"}:
        raise Fault("INVALID_CHECK_MANIFEST", "Expected workspace, checks, artifact, result_path and log_dir only.")
    # Round-trip finite canonical JSON and detach mutable caller references.
    result = json.loads(canonical_json(value))
    workspace = _absolute(result["workspace"], "workspace", directory=True)
    report = _absolute(result["result_path"], "result_path")
    log_dir = _absolute(result["log_dir"], "log_dir")
    _output_path(report, workspace)
    _output_path(log_dir, workspace)
    if report == log_dir or log_dir.is_relative_to(report):
        raise Fault("INVALID_CHECK_MANIFEST", "Result file cannot contain or replace the log directory.")
    if report.exists():
        raise Fault("RESULT_EXISTS", "A check report is immutable; use a new result path for another run.")
    if log_dir.exists() and not log_dir.is_dir():
        raise Fault("INVALID_CHECK_MANIFEST", "log_dir must be a directory.")
    artifact = result["artifact"]
    if not isinstance(artifact, dict) or not {"commit", "artifact_digest", "plan", "contracts"} <= artifact.keys():
        raise Fault("INVALID_CHECK_MANIFEST", "Artifact must bind a commit, digest, plan and contract versions.")
    if not isinstance(artifact["commit"], str) or not COMMIT_RE.fullmatch(artifact["commit"]):
        raise Fault("INVALID_CHECK_MANIFEST", "Artifact commit must be a full immutable Git object ID.")
    validate_digest(artifact["artifact_digest"])
    plan = artifact["plan"]
    if not isinstance(plan, dict) or set(plan) != {"id", "revision", "scope_digest"}:
        raise Fault("INVALID_CHECK_MANIFEST", "Plan binding is incomplete.")
    validate_identifier(plan["id"], "plan id")
    if type(plan["revision"]) is not int or plan["revision"] < 1:
        raise Fault("INVALID_CHECK_MANIFEST", "Plan revision must be a positive integer.")
    validate_digest(plan["scope_digest"])
    if not isinstance(artifact["contracts"], list) or len(artifact["contracts"]) > 256:
        raise Fault("INVALID_CHECK_MANIFEST", "Contract bindings must be a bounded list.")
    for contract in artifact["contracts"]:
        if not isinstance(contract, dict) or set(contract) != {"id", "version", "digest"}:
            raise Fault("INVALID_CHECK_MANIFEST", "Contract binding is incomplete.")
        validate_identifier(contract["id"], "contract id")
        if not isinstance(contract["version"], str) or not 0 < len(contract["version"]) <= 128:
            raise Fault("INVALID_CHECK_MANIFEST", "Contract version must be bounded text.")
        validate_digest(contract["digest"])
    checks = result["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_CHECKS:
        raise Fault("INVALID_CHECK_MANIFEST", "Supply one to 128 checks.")
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or not {"id", "criterion"} <= check.keys() or set(check) - {"id", "criterion", "argv", "cwd", "timeout_ms", "required"}:
            raise Fault("INVALID_CHECK_MANIFEST", "Check shape is invalid.")
        validate_identifier(check["id"], "check id")
        if check["id"] in ids:
            raise Fault("INVALID_CHECK_MANIFEST", "Check IDs must be unique.")
        ids.add(check["id"])
        if not isinstance(check["criterion"], str) or not 0 < len(check["criterion"]) <= 16_384:
            raise Fault("INVALID_CHECK_MANIFEST", "Check criterion must be bounded nonempty text.")
        if "required" in check and type(check["required"]) is not bool:
            raise Fault("INVALID_CHECK_MANIFEST", "required must be a boolean.")
        timeout = check.get("timeout_ms", 120_000)
        if type(timeout) is not int or not 1 <= timeout <= 3_600_000:
            raise Fault("INVALID_CHECK_MANIFEST", "Check timeout must be one millisecond to one hour.")
        if "argv" in check and (not isinstance(check["argv"], list) or not 1 <= len(check["argv"]) <= 1024
                                or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 131_072 for arg in check["argv"])
                                or not check["argv"][0]):
            raise Fault("INVALID_CHECK_MANIFEST", "argv must be a nonempty bounded string array.")
        cwd = check.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd or "\\" in cwd or "\x00" in cwd or PurePosixPath(cwd).is_absolute() or ".." in PurePosixPath(cwd).parts:
            raise Fault("INVALID_CHECK_MANIFEST", "Check cwd must be a relative path inside the workspace.")
        _absolute(str(workspace / cwd), "check cwd", directory=True)
    if not any(check.get("required", True) for check in checks):
        raise Fault("INVALID_CHECK_MANIFEST", "At least one required check is needed for a technical gate.")
    return result


def source_snapshot(workspace: Path) -> dict[str, dict]:
    """Hash all source entries, including ignored files, without following links."""
    result: dict[str, dict] = {}
    total_bytes = 0
    for folder, directories, files in os.walk(workspace, followlinks=False):
        parent = Path(folder)
        if parent == workspace:
            directories[:] = [name for name in directories if name not in {".git", SCRATCH_NAME}]
            files = [name for name in files if name not in {".git", SCRATCH_NAME}]
        for name in list(directories):
            target = parent / name
            if target.is_symlink():
                directories.remove(name)
                files.append(name)
        for name in sorted(files):
            target = parent / name
            relative = target.relative_to(workspace).as_posix()
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(target)
                try:
                    resolved = target.resolve()
                except (OSError, RuntimeError) as exc:
                    raise Fault("SOURCE_UNSAFE", "Source contains an unresolved symlink.") from exc
                if os.path.isabs(link) or not resolved.is_relative_to(workspace):
                    raise Fault("SOURCE_UNSAFE", "Source contains an escaping symlink.")
                result[relative] = {"kind": "symlink", "target": link, "mode": stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > MAX_SOURCE_BYTES:
                    raise Fault("SOURCE_TOO_LARGE", "Source snapshot exceeds two GiB.")
                hasher = hashlib.sha256()
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags)
                with os.fdopen(descriptor, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise Fault("SOURCE_CHANGED", "Source file changed while its snapshot was opened.")
                    while chunk := source.read(1024 * 1024):
                        hasher.update(chunk)
                    after = os.fstat(source.fileno())
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                        raise Fault("SOURCE_CHANGED", "Source file changed during snapshot capture.")
                result[relative] = {"kind": "file", "digest": hasher.hexdigest(), "mode": stat.S_IMODE(info.st_mode), "size": info.st_size}
            else:
                raise Fault("SOURCE_UNSAFE", "Source contains a special file.")
            if len(result) > MAX_SOURCE_FILES:
                raise Fault("SOURCE_TOO_LARGE", "Source snapshot exceeds 100000 files.")
    return result


def _git(workspace: Path, *args: str) -> bytes:
    if (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
        raise Fault("INVALID_ARTIFACT", "Checks require a private Git checkout with its own metadata.")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"})
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                        "-c", "core.fsmonitor=false", "--git-dir", str(workspace / ".git"),
                                        *args], stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Fault("INVALID_ARTIFACT", "Git could not inspect the source artifact.") from exc
        if completed.returncode:
            raise Fault("INVALID_ARTIFACT", "Git rejected the source artifact identity.")
        out.seek(0)
        value = out.read(32 * 1024 * 1024 + 1)
        if len(value) > 32 * 1024 * 1024:
            raise Fault("SOURCE_TOO_LARGE", "Git artifact inventory exceeds its size limit.")
        return value


def _verify_artifact(workspace: Path, artifact: dict, snapshot: dict) -> None:
    commit = artifact["commit"]
    current = _git(workspace, "rev-parse", "--verify", "HEAD").decode().strip()
    if current != commit:
        raise Fault("ARTIFACT_MISMATCH", "Checkout HEAD does not match the approved candidate commit.")
    listing = _git(workspace, "ls-tree", "-r", "-z", commit)
    if "sha256:" + hashlib.sha256(listing).hexdigest() != artifact["artifact_digest"]:
        raise Fault("ARTIFACT_MISMATCH", "Candidate tree digest does not match the supplied artifact.")
    expected = set()
    for record in listing.split(b"\x00"):
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii").split()
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Fault("CAPABILITY_UNSUPPORTED", "Non-UTF-8 Git paths are not supported by this checker.") from exc
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] in {".git", SCRATCH_NAME}:
            raise Fault("INVALID_ARTIFACT", "Artifact contains a reserved or invalid path.")
        expected.add(name)
        observed = snapshot.get(name)
        if observed is None or object_type != "blob" or mode not in {"100644", "100755", "120000"}:
            raise Fault("ARTIFACT_MISMATCH", "Source files do not match the committed artifact.")
        hasher = hashlib.sha1() if len(commit) == 40 else hashlib.sha256()
        if mode == "120000":
            if observed["kind"] != "symlink":
                raise Fault("ARTIFACT_MISMATCH", "Source symlink differs from the committed artifact.")
            data = observed["target"].encode("utf-8")
            hasher.update(b"blob " + str(len(data)).encode() + b"\0" + data)
        else:
            executable = bool(observed["mode"] & 0o111)
            if observed["kind"] != "file" or executable != (mode == "100755"):
                raise Fault("ARTIFACT_MISMATCH", "Source file mode differs from the committed artifact.")
            hasher.update(b"blob " + str(observed["size"]).encode() + b"\0")
            descriptor = os.open(workspace / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
        if hasher.hexdigest() != object_id:
            raise Fault("ARTIFACT_MISMATCH", "Source file content differs from the committed artifact.")
    if expected != set(snapshot):
        raise Fault("ARTIFACT_MISMATCH", "Untracked source files are present; checks must run on an exact clean artifact.")


def _file_digest(path: Path, limit: int = 512 * 1024 * 1024) -> str:
    if path.stat().st_size > limit:
        raise Fault("EVIDENCE_TOO_LARGE", "Evidence file exceeds its size limit.")
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _executable(argv: list[str], cwd: Path) -> dict:
    name = argv[0]
    candidate = Path(name) if os.path.isabs(name) else (cwd / name if os.path.dirname(name) else None)
    found = str(candidate) if candidate is not None else shutil.which(name)
    if found is None:
        raise Fault("CHECK_EXECUTABLE_MISSING", "A check executable was not found.")
    path = Path(found).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Fault("CHECK_EXECUTABLE_MISSING", "A check executable is not an executable regular file.")
    return {"path": str(path), "digest": _file_digest(path), "size": path.stat().st_size}


def _write_report(path: Path, report: dict) -> None:
    _absolute(str(path), "result_path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(canonical_json(report) + "\n")
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publish is atomic and refuses an existing immutable report.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def execute(manifest: dict, *, execution_profile: str = "cooperative_native") -> dict:
    if execution_profile not in ("cooperative_native", "controlled_executor"):
        raise Fault("INVALID_CHECK_MANIFEST", "Unknown check execution profile.")
    strict = execution_profile == "controlled_executor"
    manifest = validate_manifest(manifest)
    workspace = Path(manifest["workspace"])
    result_path, log_dir = Path(manifest["result_path"]), Path(manifest["log_dir"])
    if strict and (result_path.is_relative_to(workspace) or log_dir.is_relative_to(workspace)):
        raise Fault("INVALID_CHECK_MANIFEST", "Strict check evidence must remain outside the source sandbox.")
    scratch = workspace / SCRATCH_NAME
    scratch_preexisted = scratch.exists() or scratch.is_symlink()
    scratch_owned = False
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {"platform": platform.system(), "release": platform.release(), "machine": platform.machine(),
                   "python_version": platform.python_version(), "runner_executable": str(Path(sys.executable).resolve()),
                   "runner_executable_digest": _file_digest(Path(sys.executable).resolve()),
                   "python_bytecode_disabled": True, "source_scratch_directory": None if strict else SCRATCH_NAME,
                   "temp_and_cache": "private_per_check_sandbox" if strict else "external_per_run",
                   "execution_profile": execution_profile,
                   "environment_values_recorded": False, "hermetic_environment": False, "executables": []}
    report = {"schema_version": 1, "artifact": manifest["artifact"], "manifest_digest": digest(manifest),
              "execution_profile": execution_profile,
              "status": "failed", "checks": [], "source_unchanged": False,
              "source_before_digest": None, "source_after_digest": None,
              "started_at": utc_now(), "ended_at": None, "environment": environment,
              "environment_digest": None, "blockers": []}
    before = None
    stop_reason = None
    try:
        before = source_snapshot(workspace)
        _verify_artifact(workspace, manifest["artifact"], before)
        report["source_before_digest"] = digest(before)
    except (Fault, OSError) as exc:
        stop_reason = "artifact_validation_failed"
        report["blockers"].append(exc.as_dict() if isinstance(exc, Fault) else {"code": "IO_ERROR", "message": type(exc).__name__})
    if stop_reason is None:
        if scratch_preexisted:
            stop_reason = "reserved_scratch_already_exists"
            report["blockers"].append({"code": "SCRATCH_EXISTS", "message": "A fresh verification checkout must not contain preexisting reserved scratch."})
        elif not strict:
            _absolute(str(scratch), "check scratch")
            scratch.mkdir(mode=0o700, exist_ok=True)
            scratch_owned = True
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    if temporary_parent.is_relative_to(workspace):
        temporary_parent = Path("/tmp") if os.name != "nt" else log_dir.parent
    if temporary_parent.is_relative_to(workspace):
        raise Fault("CAPABILITY_UNSUPPORTED", "No external supervisor state directory is available.")
    execution_state = Path(tempfile.mkdtemp(prefix="xiaolvji-check-execution-", dir=temporary_parent))
    retain_state = False
    executor = ProcessExecutor(execution_state, profile=execution_profile)
    for name in ("tmp", "cache"):
        (execution_state / name).mkdir(mode=0o700)
    try:
        for check in manifest["checks"]:
            cwd = workspace / check.get("cwd", ".")
            required = check.get("required", True)
            item = {"id": check["id"], "criterion": check["criterion"], "required": required,
                    "argv": check.get("argv"), "cwd": str(cwd), "timeout_ms": check.get("timeout_ms", 120_000),
                    "status": "not_run", "started_at": None, "ended_at": None, "exit_code": None,
                    "timed_out": False, "process_tree_terminated": None, "logs": []}
            report["checks"].append(item)
            if stop_reason:
                item["reason"] = stop_reason
                continue
            if "argv" not in check:
                item["status"] = "not_run" if required else "skipped"
                item["reason"] = "No executable argv was supplied for this criterion."
                continue
            try:
                _absolute(str(cwd), "check cwd", directory=True)
                identity = _executable(check["argv"], cwd)
                item["executable"] = identity
                # Pin the exact executable whose bytes were recorded. A strict
                # sandbox has its own PATH; silently finding a different tool
                # there would make the environment evidence false.
                item["executed_argv"] = [identity["path"], *check["argv"][1:]]
                environment["executables"].append({"check_id": check["id"], **identity})
                item["started_at"] = utc_now()
                attempt_id = "check-" + uuid.uuid4().hex
                retain_state = True  # Keep launch evidence if execution becomes uncertain.
                check_env = {"PYTHONDONTWRITEBYTECODE": "1"} if strict else {
                                                 "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(execution_state / "tmp"),
                                                 "TMP": str(execution_state / "tmp"), "TEMP": str(execution_state / "tmp"),
                                                 "XDG_CACHE_HOME": str(execution_state / "cache")}
                policy = {"sandbox_policy": {"workspace": str(workspace), "write_scope": [],
                          "readonly_paths": [], "writable_paths": []}} if strict else {}
                outcome = await executor.run({"attempt_id": attempt_id, "generation": 1, "launch_key": attempt_id},
                                             item["executed_argv"], str(cwd), env=check_env,
                                             timeout=item["timeout_ms"] / 1000, **policy)
                item.update(ended_at=utc_now(), exit_code=outcome.get("exit_code"), timed_out=outcome.get("timed_out", False),
                            cancelled=outcome.get("cancelled", False),
                            process_tree_terminated=outcome.get("process_tree_terminated", False))
                item["process_identity"] = outcome.get("identity")
                if strict:
                    item["sandbox"] = outcome.get("sandbox")
                if item["process_tree_terminated"] is True:
                    retain_state = False
                for stream in ("stdout", "stderr"):
                    original = Path(outcome[stream + "_path"]) if outcome.get(stream + "_path") else None
                    if original and original.is_file():
                        target = log_dir / (check["id"] + "." + stream + ".log")
                        _absolute(str(log_dir), "log_dir", directory=True)
                        remaining = MAX_LOG_BYTES
                        overflow = False
                        with original.open("rb") as source, target.open("xb") as output:
                            while chunk := source.read(min(64 * 1024, remaining + 1)):
                                output.write(chunk[:remaining])
                                if len(chunk) > remaining:
                                    overflow = True
                                    break
                                remaining -= len(chunk)
                        item["logs"].append({"stream": stream, "path": str(target), "digest": _file_digest(target, MAX_LOG_BYTES),
                                             "bytes": target.stat().st_size,
                                             "truncated": overflow or outcome.get("log_truncated", {}).get(stream, False)})
                if item["process_tree_terminated"] is not True:
                    item["status"] = "failed"
                    item["reason"] = stop_reason = "process_tree_exit_uncertain"
                    retain_state = True
                elif item["timed_out"]:
                    item["status"] = "failed"
                    item["reason"] = stop_reason = "check_timeout"
                elif item["cancelled"]:
                    item["status"] = "failed"
                    item["reason"] = stop_reason = "check_cancelled"
                elif strict and (not item.get("sandbox") or item["sandbox"].get("attested") is not True):
                    item["status"] = "failed"
                    item["reason"] = stop_reason = "sandbox_attestation_missing"
                elif item["exit_code"] == 0:
                    item["status"] = "passed"
                else:
                    item["status"] = "failed"
                    item["reason"] = "nonzero_exit"
                # Never let a following check validate a mutated source tree.
                after_check = source_snapshot(workspace)
                _verify_artifact(workspace, manifest["artifact"], after_check)
                if before != after_check:
                    stop_reason = "source_changed"
                    item["status"] = "failed"
                    item["reason"] = stop_reason
            except (Fault, OSError) as exc:
                item["ended_at"] = utc_now()
                item["status"] = "not_run" if item["started_at"] is None else "failed"
                item["error"] = exc.as_dict() if isinstance(exc, Fault) else {"code": "IO_ERROR", "message": type(exc).__name__}
                stop_reason = "check_execution_or_evidence_failed"
                if isinstance(exc, Fault) and exc.code == "INSTANCE_UNCERTAIN":
                    retain_state = True
        if (scratch_owned and not retain_state and not result_path.is_relative_to(scratch)
                and not log_dir.is_relative_to(scratch)):
            try:
                if scratch.exists() or scratch.is_symlink():
                    _absolute(str(scratch), "check scratch", directory=True)
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise Fault("CAPABILITY_UNSUPPORTED", "Safe owned scratch cleanup is unavailable.")
                    shutil.rmtree(scratch)
                report["owned_source_scratch_removed"] = True
            except (Fault, OSError) as exc:
                report["blockers"].append(exc.as_dict() if isinstance(exc, Fault) else {"code": "SCRATCH_CLEANUP_FAILED", "message": type(exc).__name__})
        try:
            after = source_snapshot(workspace)
            _verify_artifact(workspace, manifest["artifact"], after)
            report["source_after_digest"] = digest(after)
            report["source_unchanged"] = before is not None and before == after and report["source_before_digest"] is not None
        except (Fault, OSError) as exc:
            report["blockers"].append(exc.as_dict() if isinstance(exc, Fault) else {"code": "IO_ERROR", "message": type(exc).__name__})
        report["environment_digest"] = digest(environment)
        report["ended_at"] = utc_now()
        checks_ok = all(item["status"] == "passed" if item["required"] else item["status"] in {"passed", "skipped"} for item in report["checks"])
        report["status"] = "passed" if checks_ok and report["source_unchanged"] and not report["blockers"] else "failed"
        if retain_state:
            report["supervisor_state_retained"] = str(execution_state)
        _write_report(result_path, report)
        return report
    finally:
        if not retain_state:
            shutil.rmtree(execution_state, ignore_errors=True)


def run_manifest(manifest: dict, *, execution_profile: str = "cooperative_native") -> dict:
    return asyncio.run(execute(manifest, execution_profile=execution_profile))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--execution-profile", choices=("cooperative_native", "controlled_executor"),
                        default="cooperative_native")
    arguments = parser.parse_args(argv)
    try:
        manifest = read_json(arguments.manifest)
        report = run_manifest(manifest, execution_profile=arguments.execution_profile)
        print(canonical_json({"status": report["status"], "result_path": manifest["result_path"],
                              "manifest_digest": report["manifest_digest"], "source_unchanged": report["source_unchanged"]}))
        return 0 if report["status"] == "passed" else 1
    except (Fault, OSError) as exc:
        error = exc.as_dict() if isinstance(exc, Fault) else {"code": "IO_ERROR", "message": type(exc).__name__}
        print(canonical_json({"status": "failed", "error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
