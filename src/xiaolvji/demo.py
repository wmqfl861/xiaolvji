"""Reproducible real process/Git demonstration; no model or host is invoked."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .authority import atomic_json
from .contracts import Fault, Principal, digest
from .dispatcher import Dispatcher
from .executor import ProcessExecutor
from .pipeline import Pipeline
from .store import Ledger


def _git(root, *argv):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *argv], cwd=root,
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    return result.stdout.strip()


async def run_demo(output: str | Path, *, profile: str = "cooperative_native", provider_transport: bool = False):
    if profile not in ("cooperative_native", "controlled_executor"):
        raise Fault("INVALID_ARGUMENT", "Unknown demo execution profile.")
    if type(provider_transport) is not bool or provider_transport and profile != "controlled_executor":
        raise Fault("INVALID_ARGUMENT", "The provider demonstration requires controlled execution.")
    requested = Path(output).absolute()
    root = Path(os.path.normpath(requested))
    if any(part.is_symlink() for part in (requested, *requested.parents)) or root.resolve() != root:
        raise Fault("INVALID_ARGUMENT", "The demo directory cannot pass through symbolic links.")
    try:
        root.mkdir(parents=True, mode=0o700)
    except FileExistsError as exc:
        raise Fault("DESTINATION_EXISTS", "Use a new directory for the isolated demonstration.") from exc
    repository = root / "project"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Parallel Development Demo")
    _git(repository, "config", "user.email", "demo@example.invalid")
    (repository / "src").mkdir()
    (repository / "src" / "__init__.py").write_text("", encoding="utf-8")
    # Exact file grants support in-place writes to existing files. Creating a
    # new file requires an approved directory grant; never silently widen one.
    for name in ("alpha", "beta", "gamma", "combined"):
        (repository / "src" / (name + ".py")).write_text("# Approved fixture module.\n", encoding="utf-8")
    (repository / "README.md").write_text("Isolated fixture for controller acceptance.\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "fixture baseline")
    baseline = _git(repository, "rev-parse", "HEAD")
    state = root / "state"
    state.mkdir()
    ledger = Ledger(state / "ledger.sqlite3", global_limit=2)
    operator = Principal("demo-operator", "operator")
    ledger.enable_project("demo", str(repository), {"project_limit": 2, "profile": profile, "review_authority": "independent-reviewer"}, operator, "enable-demo")
    scopes = [{"path": "src/" + name + ".py", "kind": "file"} for name in ("alpha", "beta", "gamma", "combined")]
    plan = {"id": "demo-plan", "revision": 1, "scope_digest": digest(scopes), "approved_scope": scopes,
            "approval_ref": "Built-in reversible local fixture explicitly invoked through the demo command", "contracts": []}
    ledger.register_plan("demo", plan, operator, "demo-plan")
    plan_ref = {k: plan[k] for k in ("id", "revision", "scope_digest")}
    for i, name in enumerate(("alpha", "beta", "gamma", "combined")):
        code = ("from src." + name + " import value; assert value() == " + str(i+1)) if name != "combined" else "from src.combined import value; assert value() == 3"
        task = {"schema_version": 1, "task_id": name, "plan": plan_ref, "role": "worker", "engine": "codex",
                "objective": "Implement the isolated " + name + " fixture module.", "base_commit": baseline,
                "contract_versions": [], "depends_on": ([{"task_id": n, "gate": "implementation_verified"} for n in ("alpha", "beta")] if name == "combined" else []),
                "write_scope": [scopes[i]], "exclusive_resources": [],
                "acceptance": [{"id": name+"-value", "criterion": "The approved function returns its specified value.",
                                "argv": [sys.executable, "-B", "-c", code], "cwd": ".", "timeout_ms": 5000, "required": True}],
                "expected_artifacts": ["candidate_commit", "validation_report"], "priority": 50,
                "retry": {"max_retries": 0, "backoff_ms": 0}, "final_review_required": True}
        ledger.register_task("demo", task, operator, "register-"+name)
    fixture = None
    providers = None
    if provider_transport:
        from .provider_demo import DemoProvider
        fixture = DemoProvider(state, ledger)
        providers = await fixture.start()
    def command(attempt, workspace, prompt):
        name = attempt["task_id"]
        if name == "combined":
            content = "from src.alpha import value as a\nfrom src.beta import value as b\ndef value():\n    return a() + b()\n"
        else:
            content = "def value():\n    return " + str({"alpha": 1, "beta": 2, "gamma": 3}[name]) + "\n"
        # Slow alpha gives an observable rolling refill window.
        delay = 1.3 if name == "alpha" else 0.07
        if fixture is not None:
            return fixture.command(name, content, delay)
        script = "import pathlib,time; time.sleep(" + repr(delay) + "); pathlib.Path(" + repr("src/"+name+".py") + ").write_text(" + repr(content) + ")"
        return {"argv": [sys.executable, "-B", "-c", script], "stdin_text": None, "env": {}}
    events = []
    pipeline = Pipeline(state, ledger, command_factory=command, provider_runtime=providers)
    executor = ProcessExecutor(state / "executor", profile=profile)
    dispatcher = Dispatcher(ledger, executor, pipeline, event_callback=events.append)
    try:
        result = await dispatcher.run("demo", timeout=60)
    finally:
        if fixture is not None:
            await fixture.close()
    rows = ledger.events(limit=1000)
    gamma_start = next((e["seq"] for e in rows if e["type"] == "run_start_ack" and e["task_id"] == "gamma"), None)
    alpha_exit = next((e["seq"] for e in rows if e["type"] == "run_exit_confirmed" and e["task_id"] == "alpha"), None)
    state_view = result["status"]
    originals = [t for t in state_view["tasks"] if t["task_id"] in {"alpha", "beta", "gamma", "combined"}]
    max_used = max((e["used"] for e in events if e["type"] == "capacity"), default=0)
    assertions = {"rolling_refill_before_slowest_sibling": gamma_start is not None and alpha_exit is not None and gamma_start < alpha_exit,
                  "global_limit_respected": max_used <= 2,
                  "all_four_modules_integrated_and_verified": len(originals) == 4 and all(t["state"] == "AWAITING_REVIEW" for t in originals),
                  "independent_review_not_fabricated": all(t["state"] != "ACCEPTED" for t in state_view["tasks"]),
                  "capacity_reclaimed": state_view["capacity"]["used"] == 0,
                  "user_main_ref_unchanged": _git(repository, "rev-parse", "HEAD") == baseline,
                  "user_worktree_unchanged": _git(repository, "status", "--porcelain") == "",
                  "no_controller_errors": not result["errors"]}
    transport_report = fixture.report() if fixture else None
    if transport_report is not None:
        assertions.update({"four_scoped_provider_requests": transport_report["http_requests"] == 4,
                           "provider_limit_respected": transport_report["maximum_local_provider_in_flight"] <= 2,
                           "provider_grants_revoked": transport_report["all_grants_revoked"],
                           "provider_requests_accounted": transport_report["all_requests_accounted"]})
    report = {"schema_version": 1, "status": "passed" if all(assertions.values()) else "failed",
              "host_or_model_invoked": False, "execution_profile": profile,
              "os_write_sandbox": profile == "controlled_executor", "real_processes_and_git": True, "assertions": assertions,
              "managed_worker_limit": 2, "maximum_observed_occupancy": max_used,
              "rolling_evidence": {"gamma_start_sequence": gamma_start, "alpha_exit_sequence": alpha_exit},
              "elapsed_seconds": result["elapsed_seconds"], "tasks": [{"id": t["task_id"], "state": t["state"], "blocked_reason": t["blocked_reason"]} for t in state_view["tasks"]],
              "errors": result["errors"], "report_path": str(root / "DEMO-REPORT.json")}
    if transport_report is not None:
        report["provider_transport"] = transport_report
    atomic_json(root / "DEMO-REPORT.json", report)
    atomic_json(root / "DEMO-EVENTS.json", rows)
    return report
