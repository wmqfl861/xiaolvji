"""JSON CLI for an explicitly enrolled, local resource domain."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .authority import Authority, require
from .contracts import Fault, Principal, canonical_json, digest, load_json, read_json


def parser():
    p = argparse.ArgumentParser(prog="parallel-dev", description="Persistent rolling parallel development controller")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, default=Path(os.environ.get("PARALLEL_DEV_STATE_DIR", str(Path.home()/'.local/state/xiaolvji'))))
    p.add_argument("--identity-file", type=Path, help="Capability file; never pass a token in argv")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create one local shared resource domain and separate authority keys")
    init.add_argument("--global-limit", type=int, default=4)
    doctor = sub.add_parser("doctor", help="Probe capabilities without login or trust changes")
    doctor.add_argument("--engine", choices=["codex", "zcode"])
    doctor.add_argument("--profile", choices=["controlled_executor", "cooperative_native"], default="controlled_executor")
    doctor.add_argument("--provider-config", type=Path, help="Validate an explicit snapshot without reading credentials")
    provider = sub.add_parser("provider").add_subparsers(dest="action", required=True)
    validate_provider = provider.add_parser("validate", help="Validate provider/host bindings without authenticating")
    validate_provider.add_argument("--file", type=Path, required=True)
    host = sub.add_parser("host", help="Verify an explicit standalone native deployment").add_subparsers(dest="action", required=True)
    verify_host = host.add_parser("verify", help="Run local native protocol/boundary acceptance without reading provider credentials")
    verify_host.add_argument("--project", required=True)
    verify_host.add_argument("--deployment", type=Path, required=True)
    verify_host.add_argument("--provider-config", type=Path, required=True)
    verify_host.add_argument("--output", type=Path, required=True)
    project = sub.add_parser("project").add_subparsers(dest="action", required=True)
    enable = project.add_parser("enable")
    enable.add_argument("project_id")
    enable.add_argument("root", type=Path)
    enable.add_argument("--limit", type=int, default=4)
    enable.add_argument("--profile", choices=["controlled_executor", "cooperative_native"], default="controlled_executor")
    enable.add_argument("--acknowledge-cooperative", action="store_true", help="Explicitly select trusted same-user execution without an OS sandbox")
    enable.add_argument("--review-authority", default="reviewer")
    disable = project.add_parser("disable")
    disable.add_argument("project_id")
    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    register = plan.add_parser("register")
    register.add_argument("project_id")
    register.add_argument("--file", type=Path, required=True)
    create = plan.add_parser("create", help="Register the exact scope explicitly approved by the operator")
    create.add_argument("project_id")
    create.add_argument("--id", required=True)
    create.add_argument("--revision", type=int, default=1)
    create.add_argument("--scope", action="append", required=True, metavar="PATH:KIND")
    create.add_argument("--approval-ref", required=True)
    task = sub.add_parser("task").add_subparsers(dest="action", required=True)
    register_task = task.add_parser("register")
    register_task.add_argument("project_id")
    register_task.add_argument("--file", type=Path, required=True)
    for name in ("run", "cancel"):
        c = sub.add_parser(name)
        c.add_argument("project_id", nargs="?" if name == "run" else None)
        if name == "run":
            c.add_argument("--all", action="store_true", help="Run all enabled projects in this one shared resource domain")
            c.add_argument("--timeout", type=float, default=3600)
            c.add_argument("--watch", action="store_true", help="Keep watching for newly registered eligible work until cancellation or timeout")
            c.add_argument("--no-auto-integrate", action="store_true")
            c.add_argument("--no-auto-verify", action="store_true")
            c.add_argument("--provider-config", type=Path, help="Explicit scoped provider/host configuration; never imports existing accounts")
        else:
            c.add_argument("--task")
    status = sub.add_parser("status")
    status.add_argument("--project")
    queue = sub.add_parser("queue", allow_abbrev=False,
                           help="Read advisory task ranking and blockers without reserving capacity")
    queue.add_argument("--project")
    queue.add_argument("--limit", type=int, default=100, help="Maximum entries, from 1 to 200 (default: 100)")
    metrics = sub.add_parser("metrics", allow_abbrev=False,
                             help="Read bounded controller history measurements without granting execution or review authority")
    metrics.add_argument("--project")
    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=1000)
    gate = sub.add_parser("contract-ready", help="Record a verified frozen contract, not technical acceptance")
    gate.add_argument("project_id")
    gate.add_argument("task_id")
    gate.add_argument("--digest", required=True)
    gate.add_argument("--evidence-ref", required=True)
    review = sub.add_parser("review", help="Record the designated independent reviewer's exact delivery decision")
    review.add_argument("project_id")
    review.add_argument("--delivery-digest", required=True)
    review.add_argument("--decision", choices=["approve", "reject", "rework"], required=True)
    review.add_argument("--record", required=True)
    review.add_argument("--task", action="append")
    policy = sub.add_parser("policy")
    policy.add_argument("action", choices=["inspect", "apply", "remove"])
    policy.add_argument("--project", type=Path, required=True)
    policy.add_argument("--target", default="AGENTS.md", choices=["AGENTS.md", "AGENTS.override.md"])
    package = sub.add_parser("package")
    package.add_argument("--adapter", choices=["codex", "zcode"], required=True)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    package.add_argument("--platform", choices=["posix", "windows"])
    hook = sub.add_parser("hook")
    hook.add_argument("--adapter", choices=["codex", "zcode"], required=True)
    sub.add_parser("mcp", help="Legacy MCP 2025 stdio: read tools and authenticated task registration")
    check = sub.add_parser("check-runner")
    check.add_argument("--manifest", required=True)
    check.add_argument("--execution-profile", choices=["controlled_executor", "cooperative_native"], default="cooperative_native")
    demo = sub.add_parser("demo", help="Exercise real processes/Git fixtures without invoking a model or host")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--profile", choices=["controlled_executor", "cooperative_native"], default="cooperative_native")
    demo.add_argument("--provider-transport", action="store_true", help="Exercise scoped provider IPC with a fixed local synthetic service only")
    return p


def _ledger(state):
    from .store import Ledger
    if not (state / "ledger.sqlite3").is_file():
        raise Fault("NOT_INITIALIZED", "Run parallel-dev init for this resource domain first.")
    return Ledger(state / "ledger.sqlite3")


def _hook(args):
    from .hooks import handle, normalize_input, denial
    try:
        payload = load_json(sys.stdin.buffer.read(2 * 1024 * 1024 + 1))
        normalized = normalize_input(args.adapter, payload)
        context = None
        if (args.state_dir / "ledger.sqlite3").is_file():
            ledger = _ledger(args.state_dir)
            state = ledger.status()
            cwd = normalized.get("cwd")
            if isinstance(cwd, str) and Path(cwd).is_absolute():
                cwd = Path(cwd).resolve()
                for project in state["projects"]:
                    if project["enabled"] and cwd.is_relative_to(Path(project["root"])):
                        ready = [t for t in state["tasks"] if t["project_id"] == project["project_id"] and t["state"] == "READY"]
                        context = {"enrolled": True, "project_id": project["project_id"],
                                   "profile": project["config"]["profile"], "controller_owns_queue": True,
                                   "occupied": project["capacity"]["used"], "capacity": project["capacity"]["limit"],
                                   "ready_count": len(ready), "identity_verified": False,
                                   "continuation_reserved": False, "stop_continuations": 0,
                                   "plan_revision": project["plan"]["revision"] if project["plan"] else None}
                        break
        print(canonical_json(handle(args.adapter, payload, context)))
        return 0
    except Fault as exc:
        print(canonical_json(denial("Parallel development hook input or state is invalid: " + exc.message)))
        return 2


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "hook":
            return _hook(args)
        if args.command == "check-runner":
            from .check_runner import main as checks
            return checks(["--manifest", args.manifest, "--execution-profile", args.execution_profile])
        if args.command == "doctor":
            from .doctor import doctor
            from .provider_config import load_provider_configuration
            from .native_registry import NativeRegistry
            configuration = load_provider_configuration(args.provider_config) if args.provider_config else None
            roots = {p["project_id"]: p["root"] for p in _ledger(args.state_dir).status()["projects"]} if (args.state_dir / "ledger.sqlite3").is_file() else {}
            result = doctor(args.engine, args.profile, provider_configuration=configuration,
                            native_registry=NativeRegistry(args.state_dir), project_roots=roots)
        elif args.command == "provider":
            from .provider_config import load_provider_configuration
            result = load_provider_configuration(args.file).summary()
        elif args.command == "policy":
            from .installer import install_policy
            result = install_policy(args.project, args.action, args.target)
        elif args.command == "package":
            from .installer import build_plugin
            result = build_plugin(args.adapter, args.output, args.source, target_platform=args.platform)
        elif args.command == "demo":
            from .demo import run_demo
            result = asyncio.run(run_demo(args.output, profile=args.profile, provider_transport=args.provider_transport))
        elif args.command == "init":
            from .store import Ledger
            # A validation failure must not create credentials for an invalid domain.
            if not 1 <= args.global_limit <= 1024:
                raise Fault("INVALID_ARGUMENT", "Global worker limit must be between 1 and 1024.")
            authority = Authority(args.state_dir)
            result = authority.initialize()
            ledger = Ledger(args.state_dir / "ledger.sqlite3", global_limit=args.global_limit)
            result = {**result, "capacity": ledger.status()["capacity"],
                      "notice": "Keep reviewer credentials outside worker-accessible environments. Strict mode requires a verified OS boundary."}
        else:
            state = args.state_dir.absolute()
            authority = Authority(state)
            identity_path = args.identity_file
            if args.command == "mcp" and identity_path is None:
                identity_path = state / "identities" / "reader.key"
            principal = authority.from_file(identity_path)
            ledger = _ledger(state)
            from .dispatcher import request_id
            if args.command == "mcp":
                from .mcp import serve
                return serve(ledger, principal)
            if args.command == "status":
                result = ledger.status(args.project)
            elif args.command == "queue":
                result = ledger.queue(args.project, args.limit)
            elif args.command == "metrics":
                from .metrics import runtime_metrics
                result = runtime_metrics(ledger, args.project)
            elif args.command == "events":
                result = {"events": ledger.events(args.after, args.limit)}
            elif args.command == "project":
                if args.action == "enable":
                    if args.profile == "cooperative_native" and not args.acknowledge_cooperative:
                        raise Fault("PROFILE_NOT_AUTHORIZED", "Cooperative execution requires --acknowledge-cooperative; it has no OS write sandbox.")
                    result = ledger.enable_project(args.project_id, str(args.root.resolve()),
                        {"project_limit": args.limit, "profile": args.profile, "review_authority": args.review_authority},
                        principal, request_id("project-enable"))
                else:
                    result = ledger.disable_project(args.project_id, principal, request_id("project-disable"))
            elif args.command == "plan":
                if args.action == "create":
                    scopes = []
                    for item in args.scope:
                        path, separator, kind = item.rpartition(":")
                        if not separator or kind not in ("file", "directory"):
                            raise Fault("INVALID_ARGUMENT", "Scope must be PATH:file or PATH:directory.")
                        scopes.append({"path": path, "kind": kind})
                    plan = {"id": args.id, "revision": args.revision, "scope_digest": digest(scopes),
                            "approved_scope": scopes, "approval_ref": args.approval_ref, "contracts": []}
                else:
                    plan = read_json(args.file)
                result = ledger.register_plan(args.project_id, plan, principal, request_id("plan-register"))
            elif args.command == "task":
                result = ledger.register_task(args.project_id, read_json(args.file), principal, request_id("task-register"))
            elif args.command == "host":
                require(principal, "operator")
                from .native_policy import load_native_deployment
                from .native_registry import NativeRegistry
                from .provider_config import load_provider_configuration
                project = ledger.project(args.project)
                if not project["enabled"] or project["config"]["profile"] != "controlled_executor":
                    raise Fault("CAPABILITY_UNSUPPORTED", "Native acceptance requires an enabled controlled project.")
                result = asyncio.run(NativeRegistry(state).verify(args.project, project["root"],
                    load_native_deployment(args.deployment), load_provider_configuration(args.provider_config), args.output))
            elif args.command in ("run", "cancel"):
                require(principal, "operator", "planner", "coordinator")
                if args.command == "run" and args.all:
                    if args.project_id:
                        raise Fault("INVALID_ARGUMENT", "Use a project id or --all.")
                    projects = [p for p in ledger.status()["projects"] if p["enabled"]]
                    if not projects:
                        raise Fault("NOT_FOUND", "No enabled projects.")
                    profiles = {p["config"]["profile"] for p in projects}
                    if len(profiles) != 1:
                        raise Fault("CAPABILITY_UNSUPPORTED", "A shared run requires one explicitly selected execution profile.")
                    project = projects[0]
                else:
                    if not args.project_id:
                        raise Fault("INVALID_ARGUMENT", "Specify a project id or --all.")
                    project = ledger.project(args.project_id)
                from .executor import ProcessExecutor
                from .pipeline import Pipeline
                from .dispatcher import Dispatcher
                from .native_registry import NativeRegistry
                executor = ProcessExecutor(state / "executor", profile=project["config"]["profile"])
                providers = None
                if getattr(args, "provider_config", None):
                    if project["config"]["profile"] != "controlled_executor":
                        raise Fault("CAPABILITY_UNSUPPORTED", "Scoped provider configuration requires controlled execution.")
                    from .provider_config import load_provider_configuration
                    from .provider_runtime import ProviderRuntime
                    providers = ProviderRuntime(state / "providers", ledger, load_provider_configuration(args.provider_config))
                pipeline = Pipeline(state, ledger, auto_verify=not getattr(args, "no_auto_verify", False),
                                    auto_integrate=not getattr(args, "no_auto_integrate", False), provider_runtime=providers,
                                    native_executor=executor, native_registry=NativeRegistry(state))
                dispatcher = Dispatcher(ledger, executor, pipeline,
                    event_callback=lambda event: print(canonical_json(event), file=sys.stderr, flush=True))
                if args.command == "run":
                    result = asyncio.run(dispatcher.run(args.project_id, args.timeout, watch=args.watch))
                else:
                    result = {"cancel": asyncio.run(dispatcher.cancel(args.project_id, args.task)), "status": ledger.status(args.project_id)}
            elif args.command == "contract-ready":
                require(principal, "operator", "planner")
                task = ledger.task(args.project_id, args.task_id)
                result = ledger.record_gate(args.project_id, args.task_id, "contract_ready", args.digest,
                    {"plan": task["task"]["plan"], "evidence_ref": args.evidence_ref},
                    Principal(principal.id, "planner"), request_id("contract-ready"))
            elif args.command == "review":
                require(principal, "reviewer")
                tasks = ledger.status(args.project_id)["tasks"]
                selected = set(args.task) if args.task else {t["task_id"] for t in tasks if "integrated_verified" in t["gates"]}
                if not selected or selected - {t["task_id"] for t in tasks}:
                    raise Fault("NOT_FOUND", "No matching reviewable tasks.")
                # Preflight all bindings before recording any per-task decision.
                for task in tasks:
                    if task["task_id"] in selected and task["gates"].get("integrated_verified", {}).get("artifact_digest") != args.delivery_digest:
                        raise Fault("CONTRACT_STALE", "Review digest does not match every selected delivery.")
                result = {"decisions": [ledger.review(args.project_id, task["task_id"], args.delivery_digest,
                    args.decision, {"plan": task["task"]["plan"], "review_record_id": args.record, "decision": args.decision},
                    principal, request_id("review")) for task in tasks if task["task_id"] in selected]}
            else:
                raise Fault("INVALID_ARGUMENT", "Unknown operation.")
        print(canonical_json(result))
        return 0
    except Fault as exc:
        print(canonical_json({"error": exc.as_dict()}))
        return 2
    except KeyboardInterrupt:
        print(canonical_json({"error": {"code": "CANCELLED", "message": "Interrupted; inspect status before restarting uncertain work."}}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
