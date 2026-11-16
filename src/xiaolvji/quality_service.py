"""Trusted report service for a reserved strict verification/integration task.

Only this fixed controller program runs outside the worker boundary. Every
untrusted check command enters its own offline, read-only-source sandbox. The
service's journal, manifest, report, and other checks' state are never mounted
into that sandbox. This is not a general privileged-command execution API.
"""
from __future__ import annotations

from pathlib import Path

from .authority import atomic_json
from .contracts import Fault, digest, read_json
from .executor import ProcessExecutor


def _validate_service(executor, attempt, job):
    from .check_runner import validate_manifest

    if (executor.profile != "controlled_executor" or
            attempt["project"]["config"]["profile"] != "controlled_executor" or
            job.get("kind") not in ("verification", "integration") or not job.get("recipe") or
            job.get("trusted_check_service") is not True or
            job.get("attempt_id") != attempt["attempt_id"] or
            job.get("generation") != attempt["generation"] or
            attempt["task"]["role"] not in ("verifier", "integrator")):
        raise Fault("UNAUTHORIZED", "A strict check service requires an owned controller phase recipe.")
    manifest_path = Path(job["manifest_path"])
    if manifest_path.is_symlink() or manifest_path.resolve() != manifest_path:
        raise Fault("UNSAFE_STATE", "The quality manifest must have a canonical controller path.")
    manifest = validate_manifest(read_json(manifest_path))
    expected_artifact = {"commit": job["recipe"]["source"]["commit"],
                         "artifact_digest": job["recipe"]["source"]["artifact_digest"],
                         "plan": attempt["task"]["plan"], "contracts": attempt["task"]["contract_versions"]}
    if (manifest["workspace"] != job["workspace"]["path"] or manifest["artifact"] != expected_artifact or
            manifest["checks"] != attempt["task"]["acceptance"] or
            Path(manifest["result_path"]).parent != manifest_path.parent or
            Path(manifest["log_dir"]).parent != manifest_path.parent):
        raise Fault("INVALID_CHECK_MANIFEST", "The service inputs do not bind the admitted phase task.")
    return manifest_path, manifest


async def run_quality_service(executor, attempt, job, *, on_started=None):
    from .pipeline import module_command

    journal = executor.state_dir / "launches" / attempt["attempt_id"]
    if journal.exists() or journal.is_symlink():
        raise Fault("INSTANCE_UNCERTAIN", "A quality launch journal exists; reconcile before any replacement.")
    try:
        manifest_path, manifest = _validate_service(executor, attempt, job)
    except Fault as exc:
        details = dict(exc.details) if isinstance(exc.details, dict) else {}
        raise Fault(exc.code, str(exc), {**details, "no_process_created": True}) from exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise Fault("INVALID_CHECK_MANIFEST", "The trusted service inputs are unavailable or invalid.",
                    {"no_process_created": True}) from exc
    service = ProcessExecutor(executor.state_dir, profile="cooperative_native")

    async def authorized(identity):
        atomic_json(executor.state_dir / "launches" / attempt["attempt_id"] / "service.json", {
            "role": "trusted_check_service", "check_profile": "controlled_executor",
            "manifest_path": str(manifest_path), "manifest_digest": digest(manifest),
            "identity": identity})
        if on_started:
            await on_started(identity)

    # Never use job.argv, caller environment, or task command for this service.
    # The same durable lifecycle directory supports ordinary cancel/recovery.
    return await service.run(attempt, module_command("xiaolvji.check_runner", [
        "--manifest", str(manifest_path), "--execution-profile", "controlled_executor"]),
        str(manifest_path.parent), env={"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=job.get("timeout", 3600), on_started=authorized)


def validate_strict_report(report, manifest):
    """Reject a cooperative or unbound report on the strict technical-gate path."""
    if report.get("execution_profile") != "controlled_executor":
        raise Fault("VERIFICATION_FAILED", "A strict task requires strict check execution evidence.")
    expected = {check["id"]: check for check in manifest["checks"]}
    observed = report.get("checks", [])
    if len(observed) != len(expected) or {c.get("id") for c in observed} != set(expected):
        raise Fault("VERIFICATION_FAILED", "Strict report check identities differ from the approved manifest.")
    for item in observed:
        check = expected[item["id"]]
        if item.get("status") == "skipped" and not check.get("required", True) and "argv" not in check:
            continue
        evidence = item.get("sandbox") or {}
        policy = evidence.get("policy") or {}
        executable = item.get("executable") or {}
        approved_argv = check.get("argv")
        if (item.get("status") != "passed" or item.get("argv") != check.get("argv") or
                not approved_argv or item.get("executed_argv") != [executable.get("path"), *approved_argv[1:]] or
                item.get("cwd") != str(Path(manifest["workspace"]) / check.get("cwd", ".")) or
                type(item.get("exit_code")) is not int or item["exit_code"] != 0 or item.get("timed_out") is not False or
                item.get("cancelled") is not False or
                item.get("process_tree_terminated") is not True or
                evidence.get("attested") is not True or
                evidence.get("backend") != "linux-bubblewrap-offline-v1" or
                evidence.get("policy_digest") != digest(policy) or
                policy.get("workspace") != manifest["workspace"] or
                policy.get("write_scope") != [] or policy.get("writable_paths") != [] or policy.get("readonly_paths") != [] or
                policy.get("network") != "none"):
            raise Fault("VERIFICATION_FAILED", "A check lacks exact read-only, offline sandbox evidence.")
