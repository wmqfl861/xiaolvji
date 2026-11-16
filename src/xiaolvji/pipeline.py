"""Persisted implementation -> verification -> integration -> independent review."""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from .adapters import build_command
from .authority import atomic_json
from .contracts import Fault, Principal, bytes_digest, canonical_json, digest, normalize_scope, path_in_scope, read_json
from .dispatcher import request_id
from .workspace import Workspaces

PLANNER = Principal("controller-planner", "planner")
VERIFIER = Principal("controller-verifier", "verifier")
INTEGRATOR = Principal("controller-integrator", "integrator")
EXECUTOR = Principal("controller-executor", "executor")


def module_command(module: str, args: list[str]) -> list[str]:
    # -I prevents user PYTHONPATH/site hooks from changing the trusted runner.
    code = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module(" + repr(module) + ",run_name='__main__')"
    return [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]), *args]


class Pipeline:
    def __init__(self, state_dir: str | Path, ledger, *, auto_verify=True, auto_integrate=True,
                 command_factory=None, provider_runtime=None, native_executor=None, native_registry=None):
        self.root = Path(state_dir).resolve()
        self.ledger = ledger
        self.workspaces = Workspaces(self.root / "workspaces")
        self.auto_verify, self.auto_integrate = auto_verify, auto_integrate
        self.command_factory = command_factory
        self.providers = provider_runtime
        self.native_executor = native_executor
        self.native_registry = native_registry
        for name in ("recipes", "prepared", "completed", "quality", "candidates", "deliveries", "integration-attempts", "native-results"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def _recipe_path(self, project_id, task_id):
        return self.root / "recipes" / (hashlib.sha256((project_id + ":" + task_id).encode()).hexdigest() + ".json")

    def _recipe(self, project_id, task_id):
        path = self._recipe_path(project_id, task_id)
        return read_json(path) if path.is_file() else None

    async def prepare(self, attempt):
        task, project = attempt["task"], attempt["project"]
        recipe = self._recipe(project["project_id"], task["task_id"])
        native_ready = None
        if not recipe and self.command_factory is None:
            if project["config"]["profile"] == "controlled_executor":
                if self.providers is None or self.native_registry is None:
                    raise Fault("CAPABILITY_UNSUPPORTED", "Native execution requires explicit provider configuration and fresh host acceptance.")
                native_ready = await asyncio.to_thread(self.native_registry.check, project["project_id"], task["engine"],
                    project["root"], self.providers.configuration)
            else:
                from .doctor import doctor
                capability = await asyncio.to_thread(doctor, task["engine"], project["config"]["profile"])
                if capability["effective_mode"] == "blocked":
                    raise Fault("CAPABILITY_UNSUPPORTED", "Host execution capability is unavailable.", capability)
        repository, baseline = project["root"], task["base_commit"]
        if recipe:
            repository = recipe["source"]["repository"]
            baseline = recipe["source"]["commit"]
        else:
            inputs = []
            for dependency in task["depends_on"]:
                if dependency["gate"] != "contract_ready":
                    upstream = self.ledger.task(project["project_id"], dependency["task_id"])
                    consumed = next((d for d in attempt.get("dependency_snapshot", [])
                                     if d["task_id"] == dependency["task_id"] and d["gate"] == dependency["gate"]), None)
                    if not consumed:
                        raise Fault("DEPENDENCY_BLOCKED", "Dependency has no reserved immutable input binding.")
                    # Resolve exactly what admission reserved, including valid
                    # historical integration/review proofs, never today's head.
                    gate = self.ledger.gate(project["project_id"], dependency["task_id"], dependency["gate"],
                                            consumed["artifact_digest"])
                    candidate = upstream["candidate"]
                    if dependency["gate"] in ("integrated_verified", "final_accepted"):
                        gate = self.ledger.gate(project["project_id"], dependency["task_id"], "integrated_verified",
                                                consumed["artifact_digest"])
                        binding = gate.get("binding", {})
                        if not binding.get("integration_commit") or not binding.get("integration_repository"):
                            raise Fault("DEPENDENCY_BLOCKED", "Integrated dependency objects are unavailable.")
                        candidate = {"commit": binding["integration_commit"],
                                     "repository": binding["integration_repository"],
                                     "artifact_digest": gate["artifact_digest"]}
                    if not candidate or not candidate.get("repository"):
                        raise Fault("DEPENDENCY_BLOCKED", "Dependency candidate objects are unavailable.")
                    inputs.append(candidate)
            if inputs:
                from .integration import Composer
                combined = await asyncio.to_thread(Composer(self.root / "composition").compose,
                                                   repository, baseline, inputs)
                repository, baseline = combined["repository"], combined["commit"]
        workspace = await asyncio.to_thread(self.workspaces.create, repository, attempt["attempt_id"], baseline)
        strict = project["config"]["profile"] == "controlled_executor"
        if strict and not recipe:
            # Only the controller creates missing *declared directory* mount
            # roots. Never broaden a missing file grant to its parent directory.
            for scope in normalize_scope(workspace["path"], task["write_scope"]):
                if scope["kind"] == "directory":
                    Path(scope["canonical"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        if native_ready is not None:
            # Check after creating declared directory roots too: a declaration
            # for .codex must not create an unsupported policy after preflight.
            native_ready = await asyncio.to_thread(self.native_registry.check, project["project_id"], task["engine"],
                project["root"], self.providers.configuration, workspace=workspace["path"])
            if any(path_in_scope(path, task["write_scope"]) for path in native_ready["instructions"]):
                raise Fault("NATIVE_PROJECT_POLICY_UNSUPPORTED", "Native instruction files cannot lie inside a worker write grant.")
        record = {"workspace": workspace, "kind": recipe["kind"] if recipe else "implementation",
                  "recipe": recipe, "project_id": project["project_id"], "task_id": task["task_id"],
                  "attempt_id": attempt["attempt_id"], "generation": attempt["generation"]}
        if not recipe and self.command_factory is None:
            record["native_engine"] = task["engine"]
        launch_extras = {}
        if recipe:
            quality = self.root / "quality" / attempt["attempt_id"]
            quality.mkdir(mode=0o700)
            artifact = {"commit": baseline, "artifact_digest": recipe["source"]["artifact_digest"],
                        "plan": task["plan"], "contracts": task["contract_versions"]}
            manifest = {"workspace": workspace["path"], "checks": task["acceptance"], "artifact": artifact,
                        "result_path": str(quality / "report.json"), "log_dir": str(quality / "logs")}
            atomic_json(quality / "manifest.json", manifest)
            record["manifest_path"] = str(quality / "manifest.json")
            command = {"argv": module_command("xiaolvji.check_runner", ["--manifest", record["manifest_path"]]),
                       "stdin_text": None, "env": {"PYTHONDONTWRITEBYTECODE": "1"}}
            if strict:
                # The trusted report service runs outside each check's sandbox.
                # A test receives neither the report directory nor this manifest.
                record["trusted_check_service"] = True
            timeout = min(86400, sum(c.get("timeout_ms", 120000) for c in task["acceptance"]) / 1000 + 120)
        else:
            prompt = self._prompt(attempt, workspace)
            if native_ready is not None:
                from .native_acceptance import native_command, runner_source, native_readonly_paths
                command = native_command(native_ready["deployment"], native_ready["host_snapshot"], workspace["path"], prompt, [])
                command["native_protocol"] = native_ready["deployment"].value["protocol"]
                record["native_binding"] = {"record_digest": native_ready["record_digest"],
                    "identity_digest": digest(native_ready["identity"]),
                    "protocol": native_ready["deployment"].value["protocol"],
                    "project_policy_digest": native_ready["project_policy_digest"]}
            else:
                command = self.command_factory(attempt, workspace, prompt) if self.command_factory else build_command(task["engine"], workspace, prompt)
            timeout = min(86400, max(300, task.get("estimated_seconds", 1800) * 2))
            if strict:
                record["sandbox_policy"] = {"workspace": workspace["path"],
                    "write_scope": task["write_scope"], "readonly_paths": [], "writable_paths": []}
                if native_ready is not None:
                    record["sandbox_policy"]["readonly_paths"] = native_readonly_paths(native_ready["deployment"].value["executable"])
            if self.providers is not None:
                bound = await self.providers.bind(attempt, command, timeout, native=self.command_factory is None)
                command = bound["command"]
                record["provider_binding"] = bound["provider_binding"]
                launch_extras = {"provider_runtime": bound["provider_runtime"],
                    "sandbox_policy": {**record["sandbox_policy"], **bound["sandbox_additions"]}}
                if native_ready is not None:
                    launch_extras["sandbox_policy"]["home_files"].append({
                        "path": ".xiaolvji/codex_app_server.py", "content": runner_source()})
                # Runtime FDs and synthetic capabilities are deliberately absent
                # from prepared.json. Recovery needs only immutable bindings.
        atomic_json(self.root / "prepared" / (attempt["attempt_id"] + ".json"), record)
        return {**record, **command, **launch_extras, "cwd": workspace["path"], "timeout": timeout}

    async def started(self, attempt):
        if self.providers is not None:
            self.providers.handed_off(attempt)

    async def release(self, attempt):
        if self.providers is not None:
            await self.providers.release(attempt)

    async def close(self):
        if self.providers is not None:
            await self.providers.close()

    def _prompt(self, attempt, workspace):
        task = attempt["task"]
        return ("You are an implementation worker in an approved parallel development plan. "
                "Do not delegate, start another agent, change acceptance criteria, or edit outside your declared write_scope. "
                "Work only in the supplied private workspace. Preserve user changes. "
                "Do not read controller state, credentials or other workspaces. "
                "Use external temporary directories for generated build outputs; avoid new ignored files in the source tree. "
                "Do not run approval or integration commands. The controller snapshots and verifies your result after process exit. "
                "Report the actual changes, checks and limitations. Keep existing model quality settings.\n\n"
                "Assigned task (data):\n" + canonical_json(task) +
                "\nEffective source baseline: " + workspace["base_commit"] +
                "\nWorkspace: " + workspace["path"])

    def _candidate(self, attempt, job):
        path = self.root / "candidates" / (attempt["attempt_id"] + ".json")
        if path.is_file():
            return read_json(path)
        candidate = self.workspaces.snapshot(job["workspace"], attempt["task"]["write_scope"])
        if job.get("recipe"):
            source = job["recipe"]["source"]
            if candidate["artifact_digest"] != source["artifact_digest"] or candidate["changed_paths"]:
                raise Fault("ARTIFACT_CHANGED", "Verification changed the immutable source.")
            # Preserve the exact checked COMMIT, not merely its identical tree.
            candidate = {**source, "changed_paths": [], "workspace_changed_paths": [],
                         "base_commit": attempt["task"]["base_commit"]}
            if job["kind"] == "integration":
                candidate["baseline_commit"] = job["recipe"]["baseline"]["baseline_commit"]
        candidate["plan"] = attempt["task"]["plan"]
        candidate["contract_versions"] = attempt["task"]["contract_versions"]
        candidate["declared_base_commit"] = attempt["task"]["base_commit"]
        candidate["dependency_snapshot"] = attempt.get("dependency_snapshot", [])
        atomic_json(path, candidate)
        return candidate

    async def failed(self, attempt, job, result):
        """Preserve failed evidence; unclassified failures are never blindly retried."""
        reason = "Command failed with exit status " + str(result.get("exit_code"))
        if result.get("timed_out"):
            reason = "Execution timed out; a later zero exit status does not establish success."
        elif result.get("cancelled"):
            reason = "Execution was cancelled; no candidate may be accepted."
        if job and job.get("manifest_path"):
            manifest = read_json(job["manifest_path"])
            report_path = Path(manifest["result_path"])
            if report_path.exists():
                report = read_json(report_path)
                reason = "Required verification failed; inspect " + str(report_path)
            if job.get("kind") == "verification":
                self.ledger.block_task(attempt["project_id"], job["recipe"]["target_task"], reason,
                                       Principal("controller-coordinator", "coordinator"), request_id("verification-blocked"))
        # An exhausted budget is already blocked. An otherwise retryable
        # nonzero CLI exit needs diagnosis before a replacement task is approved.
        self.ledger.block_task(attempt["project_id"], attempt["task_id"], reason,
                               Principal("controller-coordinator", "coordinator"), request_id("execution-blocked"))

    async def finish(self, attempt, job, result):
        if result.get("timed_out") or result.get("cancelled") or result.get("exit_code") != 0:
            raise Fault("EXECUTION_FAILED", "Only a successful, non-timeout, non-cancelled run can produce a candidate.")
        finished = self.root / "completed" / (attempt["attempt_id"] + ".json")
        if finished.is_file():
            return
        project_id, task_id = attempt["project_id"], attempt["task_id"]
        native_report = None
        if "native_engine" in job:
            # Reconcile the executor-owned journal even during ordinary finish.
            # Recovery must not turn its lossy {exit_code, outcome} ledger view
            # into an assumed complete, untruncated native-host transcript.
            if self.native_executor is None or job["native_engine"] != attempt["task"]["engine"]:
                raise Fault("NATIVE_RESULT_INVALID", "Native execution evidence is not bound to its executor and engine.")
            current = self.ledger.attempt(attempt["attempt_id"])
            if not current.get("identity"):
                raise Fault("NATIVE_RESULT_INVALID", "Native execution lacks its durable process identity.")
            observed = await asyncio.to_thread(self.native_executor.reconcile, current["identity"])
            if (observed.get("process_tree_terminated") is not True or observed.get("exit_code") != 0 or
                    observed.get("cancelled") is not False or observed.get("timed_out") is not False or
                    type((observed.get("log_truncated") or {}).get("stdout")) is not bool):
                raise Fault("NATIVE_RESULT_INVALID", "Native execution lacks complete successful executor evidence.")
            from .native_result import verify_host_output
            native_report = await asyncio.to_thread(verify_host_output, job["native_engine"],
                observed.get("stdout_path"), truncated=observed["log_truncated"]["stdout"])
            if "native_binding" in job:
                from .native_policy import capture_project_policy
                policy = await asyncio.to_thread(capture_project_policy, attempt["project"]["root"], workspace=job["workspace"]["path"])
                if policy["policy_digest"] != job["native_binding"]["project_policy_digest"]:
                    raise Fault("NATIVE_PROJECT_POLICY_UNSUPPORTED", "Native project instructions changed during implementation.")
            atomic_json(self.root / "native-results" / (attempt["attempt_id"] + ".json"), {
                "attempt_id": attempt["attempt_id"], "generation": attempt["generation"],
                "identity_digest": digest(current["identity"]), "report": native_report})
        candidate = await asyncio.to_thread(self._candidate, attempt, job)
        if native_report is not None:
            candidate["native_result_digest"] = digest(native_report)
        self.ledger.submit_candidate(attempt["attempt_id"], attempt["generation"], candidate,
                                     EXECUTOR, "candidate:" + attempt["attempt_id"])
        if job["kind"] == "implementation":
            if self.auto_verify:
                self._enqueue_verification(attempt, candidate)
        else:
            manifest = read_json(job["manifest_path"])
            report = read_json(manifest["result_path"])
            if (report.get("status") != "passed" or report.get("source_unchanged") is not True or
                    report.get("artifact") != manifest["artifact"] or
                    report.get("manifest_digest") != digest(manifest)):
                raise Fault("VERIFICATION_FAILED", "Quality report is missing or does not bind this exact immutable source.")
            expected_checks = {c["id"] for c in manifest["checks"] if c.get("required", True)}
            passed = {c["id"] for c in report.get("checks", []) if c.get("status") == "passed"}
            if not expected_checks <= passed:
                raise Fault("VERIFICATION_FAILED", "Required checks did not all pass.")
            if attempt["project"]["config"]["profile"] == "controlled_executor":
                from .quality_service import validate_strict_report
                validate_strict_report(report, manifest)
            evidence = {"id": attempt["attempt_id"], "digest": bytes_digest(Path(manifest["result_path"]).read_bytes()),
                        "path": manifest["result_path"]}
            binding = {"plan": attempt["task"]["plan"], "evidence_refs": [evidence],
                       "contract_versions": attempt["task"]["contract_versions"],
                       "environment_digest": report["environment_digest"]}
            self.ledger.record_gate(project_id, task_id, "implementation_verified", candidate["artifact_digest"],
                                    binding, VERIFIER, "verify-self:" + attempt["attempt_id"])
            if job["kind"] == "verification":
                target = job["recipe"]["target_task"]
                self.ledger.record_gate(project_id, target, "implementation_verified",
                                        job["recipe"]["source"]["artifact_digest"], binding,
                                        VERIFIER, "verify-target:" + attempt["attempt_id"])
            elif job["kind"] == "integration":
                await asyncio.to_thread(self._publish, attempt, job, candidate, binding)
        atomic_json(finished, {"attempt_id": attempt["attempt_id"], "candidate": candidate["artifact_digest"], "processed": True})

    def _enqueue_verification(self, attempt, candidate):
        target = attempt["task"]
        task_id = "verify-" + hashlib.sha256((attempt["attempt_id"] + candidate["artifact_digest"]).encode()).hexdigest()[:32]
        recipe = {"kind": "verification", "target_task": target["task_id"], "source": candidate}
        task = self._internal_task(task_id, target, "verifier", candidate["commit"], target["acceptance"])
        # Explicit resource requirements for real checks remain part of admission.
        task["exclusive_resources"] = list(target["exclusive_resources"])
        atomic_json(self._recipe_path(attempt["project_id"], task_id), recipe)
        self.ledger.register_task(attempt["project_id"], task, PLANNER, "register:" + attempt["project_id"] + ":" + task_id)

    @staticmethod
    def _internal_task(task_id, source, role, baseline, checks):
        return {"schema_version": 1, "task_id": task_id, "plan": source["plan"], "role": role,
                "engine": source["engine"], "objective": "Verify the exact immutable candidate against the approved acceptance criteria.",
                "base_commit": baseline, "contract_versions": source["contract_versions"], "depends_on": [],
                "write_scope": [], "exclusive_resources": [], "acceptance": checks,
                "expected_artifacts": ["validation_report"], "retry": {"max_retries": 0, "backoff_ms": 0},
                "priority": 100, "final_review_required": True}

    async def discover(self, project_id=None):
        """Create useful phase work from verified events, never invent product scope."""
        if not self.auto_integrate:
            return False
        status = self.ledger.status(project_id)
        changed = False
        for project in status["projects"]:
            if not project["enabled"] or not project["plan"]:
                continue
            tasks = [t for t in status["tasks"] if t["project_id"] == project["project_id"] and
                     t["task"]["plan"] == {k: project["plan"][k] for k in ("id", "revision", "scope_digest")}]
            all_originals = [t for t in tasks if self._recipe(project["project_id"], t["task_id"]) is None]
            originals = [t for t in all_originals if "implementation_verified" in t["gates"] and
                         t["state"] not in {"BLOCKED", "CANCELLED"}]
            if not originals:
                continue
            # Integrate a verified frontier when a downstream node needs an
            # integration/review gate. Do not impose an all-original barrier.
            verified_ids = {t["task_id"] for t in originals}
            needs_frontier = any(d["task_id"] in verified_ids and
                d["gate"] in {"integrated_verified", "final_accepted"}
                for t in all_originals if t["task_id"] not in verified_ids for d in t["task"]["depends_on"])
            if len(originals) != len(all_originals) and not needs_frontier:
                continue
            candidate_set = [{"task_id": t["task_id"], "candidate_digest": t["candidate"]["artifact_digest"],
                              "commit": t["candidate"]["commit"]} for t in originals]
            key = digest({"project": project["project_id"], "plan": originals[0]["task"]["plan"], "candidates": candidate_set})[7:]
            task_id = "integrate-" + key[:32]
            recipe_path = self._recipe_path(project["project_id"], task_id)
            if recipe_path.exists():
                # A durable recipe is a registration outbox, not proof that
                # registration committed before a controller crash.
                saved = read_json(recipe_path)
                if "task_definition" in saved:
                    try:
                        self.ledger.task(project["project_id"], task_id)
                    except Fault as exc:
                        if exc.code != "NOT_FOUND":
                            raise
                        self.ledger.register_task(project["project_id"], saved["task_definition"], PLANNER,
                                                  "register:" + project["project_id"] + ":" + task_id)
                        if saved["kind"] == "integration_conflict":
                            self.ledger.block_task(project["project_id"], task_id, saved["error"]["message"],
                                Principal("controller-coordinator", "coordinator"), request_id("integration-blocked"))
                        changed = True
                continue
            from .integration import Composer, integration_baseline
            baseline = await asyncio.to_thread(integration_baseline, project["root"])
            try:
                source = await asyncio.to_thread(Composer(self.root / "composition").compose,
                                                 project["root"], baseline["baseline_commit"], [t["candidate"] for t in originals])
            except Fault as exc:
                atomic_json(self.root / "integration-attempts" / (key + ".json"), {"candidate_set": candidate_set, "error": exc.as_dict()})
                # Publish a real blocked phase task, visible in status/events.
                blocked = self._internal_task(task_id, originals[0]["task"], "integrator", baseline["baseline_commit"],
                    [{"id": "resolve-integration", "criterion": "Resolve the recorded integration conflict and reverify a new candidate.", "required": True}])
                atomic_json(recipe_path, {"kind": "integration_conflict", "candidate_set": candidate_set,
                                         "error": exc.as_dict(), "task_definition": blocked})
                self.ledger.register_task(project["project_id"], blocked, PLANNER, "register:" + project["project_id"] + ":" + task_id)
                self.ledger.block_task(project["project_id"], task_id, exc.message,
                    Principal("controller-coordinator", "coordinator"), request_id("integration-blocked"))
                changed = True
                continue
            checks = []
            for item in originals:
                for check in item["task"]["acceptance"]:
                    value = dict(check)
                    value["id"] = "check-" + hashlib.sha256((item["task_id"] + ":" + check["id"]).encode()).hexdigest()[:32]
                    checks.append(value)
            recipe = {"kind": "integration", "source": source, "targets": candidate_set,
                      "baseline": baseline, "repository": project["root"]}
            task = self._internal_task(task_id, originals[0]["task"], "integrator", source["commit"], checks)
            task["exclusive_resources"] = sorted({"integration:" + digest(project["root"])[7:]} |
                                                {r for t in originals for r in t["task"]["exclusive_resources"]})
            # Keep every declared interface binding, not just the first worker's.
            contracts = {c["id"]: c for t in originals for c in t["task"]["contract_versions"]}
            task["contract_versions"] = list(contracts.values())
            recipe["task_definition"] = task
            atomic_json(recipe_path, recipe)
            self.ledger.register_task(project["project_id"], task, PLANNER, "register:" + project["project_id"] + ":" + task_id)
            changed = True
        return changed

    def _publish(self, attempt, job, candidate, evidence_binding):
        publication_id = attempt["attempt_id"]
        self.ledger.begin_publication(attempt["project_id"], publication_id, attempt["task"]["plan"],
                                      INTEGRATOR, request_id("publication-begin"))
        # A crash or uncertain publication intentionally retains this lease.
        # Recovery reruns the same publication id and verifies its durable intent.
        try:
            self._publish_owned(attempt, job, candidate, evidence_binding)
        except Exception as exc:
            # All synchronous Git calls returned (or were killed and waited
            # after timeout). Preserve the failed publication for diagnosis;
            # an actual process crash bypasses this handler and retains lease.
            atomic_json(self.root / "integration-attempts" / (publication_id + "-failure.json"),
                        {"publication_id": publication_id, "message": str(exc), "candidate": candidate})
            self.ledger.end_publication(attempt["project_id"], publication_id,
                                        INTEGRATOR, request_id("publication-failed"))
            raise
        self.ledger.end_publication(attempt["project_id"], publication_id,
                                    INTEGRATOR, request_id("publication-end"))

    def _publish_owned(self, attempt, job, candidate, evidence_binding):
        from .integration import publish, integration_baseline
        from .workspace import artifact_digest
        recipe = job["recipe"]
        baseline = recipe["baseline"]
        receipt_path = self.root / "deliveries" / (attempt["task_id"] + ".json")
        intent_path = self.root / "integration-attempts" / (attempt["attempt_id"] + ".json")
        intent = {"project_id": attempt["project_id"], "plan": attempt["task"]["plan"], "baseline": baseline,
                  "candidate": candidate, "targets": recipe["targets"]}
        existed = intent_path.is_file()
        if existed and read_json(intent_path) != intent:
            raise Fault("REVISION_CONFLICT", "Durable publication intent differs from this candidate.")
        if not existed:
            atomic_json(intent_path, intent)
        current = integration_baseline(recipe["repository"], baseline["ref"])
        if receipt_path.is_file():
            receipt = read_json(receipt_path)
            if receipt["commit"] != candidate["commit"] or current["expected_commit"] != candidate["commit"]:
                raise Fault("REVISION_CONFLICT", "Published integration reference changed before gate recovery.")
        else:
            if existed and current["expected_commit"] == candidate["commit"]:
                # Git CAS committed but receipt write was interrupted. Exact
                # persisted intent + immutable commit/digest proves this result.
                if artifact_digest(candidate["repository"], candidate["commit"]) != candidate["artifact_digest"]:
                    raise Fault("ARTIFACT_CHANGED", "Publication recovery candidate changed.")
                receipt = {"published": True, "recovered": True, "ref": baseline["ref"],
                           "previous_commit": baseline["expected_commit"]}
            else:
                receipt = publish(recipe["repository"], baseline["ref"], baseline["expected_commit"], candidate)
            receipt = {**receipt, "artifact_digest": candidate["artifact_digest"], "commit": candidate["commit"],
                       "repository": recipe["repository"], "plan": attempt["task"]["plan"], "targets": recipe["targets"],
                       "review_status": "awaiting_independent_review"}
            atomic_json(receipt_path, receipt)
        for target in recipe["targets"]:
            binding = {**evidence_binding, "candidate_digest": target["candidate_digest"],
                       "integration_ref": baseline["ref"], "integration_commit": candidate["commit"],
                       "integration_repository": candidate["repository"], "baseline_commit": baseline["baseline_commit"]}
            self.ledger.record_gate(attempt["project_id"], target["task_id"], "integrated_verified", candidate["artifact_digest"],
                                    binding, INTEGRATOR, "integrated:" + attempt["attempt_id"] + ":" + target["task_id"])
        # Phase jobs are also technical artifacts of the same reviewed delivery.
        for item in self.ledger.status(attempt["project_id"])["tasks"]:
            if item["task"]["role"] not in ("verifier", "integrator") or "implementation_verified" not in item["gates"]:
                continue
            if item["task"]["plan"] != attempt["task"]["plan"]:
                continue
            binding = {**evidence_binding, "candidate_digest": item["candidate"]["artifact_digest"],
                       "integration_ref": baseline["ref"], "integration_commit": candidate["commit"],
                       "integration_repository": candidate["repository"]}
            self.ledger.record_gate(attempt["project_id"], item["task_id"], "integrated_verified", candidate["artifact_digest"],
                                    binding, INTEGRATOR, "integrated-phase:" + attempt["attempt_id"] + ":" + item["task_id"])

    async def recover_attempt(self, attempt, result):
        prepared = self.root / "prepared" / (attempt["attempt_id"] + ".json")
        if prepared.is_file() and not self.ledger.attempt(attempt["attempt_id"])["cancel_requested"]:
            if result.get("exit_code") == 0 and not result.get("timed_out") and not result.get("cancelled"):
                await self.finish(attempt, read_json(prepared), result)
            else:
                await self.failed(attempt, read_json(prepared), result)

    async def recover_completed(self, project_id=None):
        errors = []
        for prepared in (self.root / "prepared").glob("*.json"):
            if (self.root / "completed" / prepared.name).exists():
                continue
            attempt = None
            try:
                job = read_json(prepared)
                if project_id and job["project_id"] != project_id:
                    continue
                attempt = self.ledger.attempt(job["attempt_id"])
                current = self.ledger.task(attempt["project_id"], attempt["task_id"])
                if current["state"] == "BLOCKED" or current["latest_attempt"] != attempt["attempt_id"]:
                    continue
                if attempt["state"] == "RELEASED" and not attempt["cancel_requested"]:
                    outcome = attempt.get("outcome") or {}
                    if attempt["exit_code"] == 0 and not outcome.get("timed_out") and not outcome.get("cancelled"):
                        await self.finish(attempt, job, {"exit_code": 0, **outcome})
                    else:
                        await self.failed(attempt, job, {"exit_code": attempt["exit_code"], **outcome})
            except Exception as exc:
                error = {"record": str(prepared), "message": str(exc)}
                errors.append(error)
                if attempt:
                    try:
                        self.ledger.block_task(attempt["project_id"], attempt["task_id"], str(exc)[:8000] or "Recovery failed",
                                               Principal("controller-coordinator", "coordinator"), request_id("recovery-blocked"))
                    except Fault:
                        pass  # Stale/cancelled attempts cannot be reopened.
        return errors
