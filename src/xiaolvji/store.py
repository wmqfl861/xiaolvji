"""Transactional resource-domain ledger.

The caller is a trusted controller. Principals must already have been authenticated;
JSON fields such as ``producer`` or ``role`` are never converted into principals here.
Each public operation opens its own connection, so independent controllers/processes
share the same SQLite admission lock and persisted capacity budget.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Callable

from . import scheduling
from .contracts import (
    Fault, Principal, canonical_json, digest, normalize_scope, path_in_scope,
    scopes_overlap, utc_now, validate_task,
)

ACTIVE = ("RESERVED", "STARTING", "RUNNING", "STOPPING", "LOST")
ACTIVE_SQL = "('RESERVED','STARTING','RUNNING','STOPPING','LOST')"
GATES = ("contract_ready", "implementation_verified", "integrated_verified", "final_accepted")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_COMMIT = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_QUEUE_BLOCKER_LIMIT = 16
_QUEUE_MAX_TASKS = 10_000
_QUEUE_MAX_PROJECTS = 10_000
_QUEUE_MAX_ACTIVE_ATTEMPTS = 20_000
_QUEUE_MAX_LEASES = 20_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, root TEXT NOT NULL, config TEXT NOT NULL,
 enabled INTEGER NOT NULL, plan TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
 project_id TEXT NOT NULL REFERENCES projects(id), task_id TEXT NOT NULL,
 definition TEXT NOT NULL, scopes TEXT NOT NULL, state TEXT NOT NULL,
 reason TEXT, created_seq INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
 not_before REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
 candidate TEXT, latest_attempt TEXT,
 PRIMARY KEY(project_id, task_id)
);
CREATE TABLE IF NOT EXISTS attempts(
 attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
 generation INTEGER NOT NULL, state TEXT NOT NULL, identity TEXT,
 exit_code INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0,
 launch_key TEXT NOT NULL UNIQUE, dependency_snapshot TEXT NOT NULL,
 terminal_observation TEXT, created_seq INTEGER NOT NULL,
 UNIQUE(project_id,task_id,generation),
 FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,task_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON attempts(project_id,task_id)
 WHERE state IN ('RESERVED','STARTING','RUNNING','STOPPING','LOST');
CREATE TABLE IF NOT EXISTS outbox(
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
 launch_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases(
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 kind TEXT NOT NULL, resource_key TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(attempt_id,kind,resource_key)
);
CREATE TABLE IF NOT EXISTS gates(
 project_id TEXT NOT NULL, task_id TEXT NOT NULL, gate TEXT NOT NULL,
 artifact_digest TEXT NOT NULL, binding TEXT NOT NULL,
 dependencies TEXT NOT NULL, principal_id TEXT NOT NULL, created_seq INTEGER NOT NULL,
 PRIMARY KEY(project_id,task_id,gate),
 FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,task_id)
);
CREATE TABLE IF NOT EXISTS gate_history(
 created_seq INTEGER PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
 gate TEXT NOT NULL, artifact_digest TEXT NOT NULL, binding TEXT NOT NULL,
 dependencies TEXT NOT NULL, principal_id TEXT NOT NULL, epoch INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS gate_history_lookup ON gate_history(project_id,task_id,gate,artifact_digest,epoch);
CREATE TABLE IF NOT EXISTS gate_history_epochs(
 project_id TEXT NOT NULL, task_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 PRIMARY KEY(project_id,task_id)
);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
 project_id TEXT, task_id TEXT, attempt_id TEXT,
 occurred_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
 request_id TEXT PRIMARY KEY, body_digest TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), publication_id TEXT NOT NULL,
 plan TEXT NOT NULL, principal_id TEXT NOT NULL, created_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempt_project ON attempts(project_id,state);
CREATE INDEX IF NOT EXISTS task_state ON tasks(project_id,state);
"""


def _json(text: str | None) -> Any:
    return json.loads(text) if text is not None else None


def _integer(value: Any, name: str, minimum: int = 1, maximum: int = 1024) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Fault("INVALID_REQUEST", f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _identifier(value: Any, name: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise Fault("INVALID_REQUEST", f"Invalid {name}")
    return value


def _artifact(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise Fault("INVALID_REQUEST", "An exact sha256 artifact digest is required")
    return value


def _plan_binding(plan: dict) -> dict:
    return {k: plan[k] for k in ("id", "revision", "scope_digest")}


@dataclass
class _Admission:
    """Shared decision detail; only the compact summaries leave queue()."""
    eligible: bool
    scopes: list[dict] = field(default_factory=list)
    resources: set[str] = field(default_factory=set)
    snapshot: list[dict] = field(default_factory=list)
    generation: int = 0
    fault: Fault | None = None
    blockers: list[dict] = field(default_factory=list)
    blocker_count: int = 0

    def block(self, fault: Fault, code: str, kind: str, **details: Any) -> None:
        if self.fault is None:
            self.fault = fault
        self.blocker_count += 1
        if len(self.blockers) < _QUEUE_BLOCKER_LIMIT:
            self.blockers.append({"code": code, "kind": kind, **details})


class Ledger:
    """Persistent admission, delivery state, and evidence bindings for one domain."""

    def __init__(self, path: str | Path, global_limit: int | None = None):
        proposed = Path(path).expanduser().absolute()
        if proposed.is_symlink():
            raise Fault("INVALID_REQUEST", "Ledger path must not be a symlink")
        proposed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = proposed.resolve()
        if self.path.exists() and not self.path.is_file():
            raise Fault("INVALID_REQUEST", "Ledger path must be a regular file")
        if global_limit is not None:
            _integer(global_limit, "global_limit")
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()
            if current is None:
                conn.execute("INSERT INTO settings VALUES('global_limit',?)", (str(global_limit or 4),))
                conn.execute("INSERT OR IGNORE INTO settings VALUES('schema_version','1')")
            elif global_limit is not None and int(current[0]) != global_limit:
                raise Fault("REVISION_CONFLICT", "Persisted global capacity differs; it is never changed by reopening the ledger")
            conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # Filesystem mode bits alone are not the execution boundary.

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _authorize(principal: Principal, allowed: set[str]) -> None:
        if not isinstance(principal, Principal) or not principal.id or principal.role not in allowed:
            raise Fault("UNAUTHORIZED", "This operation requires a separately authenticated authority")

    def _mutate(self, operation: str, body: dict, principal: Principal, request_id: str,
                roles: set[str], change: Callable[[sqlite3.Connection], Any]) -> Any:
        self._authorize(principal, roles)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise Fault("INVALID_REQUEST", "A bounded idempotency request_id is required")
        envelope = {"operation": operation, "body": body,
                    "principal": {"id": principal.id, "role": principal.role}}
        raw = canonical_json(envelope)
        if len(raw.encode("utf-8")) > 1_048_576:
            raise Fault("INVALID_REQUEST", "Request exceeds one MiB")
        body_digest = digest(envelope)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            recorded = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if recorded:
                if recorded["body_digest"] != body_digest:
                    raise Fault("IDEMPOTENCY_CONFLICT", "Idempotency key already belongs to another request")
                result = _json(recorded["result"])
                conn.commit()
                return result
            result = change(conn)
            conn.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, body_digest, canonical_json(result)))
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _event(conn: sqlite3.Connection, kind: str, project_id: str | None = None,
               task_id: str | None = None, attempt_id: str | None = None, **payload: Any) -> int:
        cur = conn.execute(
            "INSERT INTO events(type,project_id,task_id,attempt_id,occurred_at,payload) VALUES(?,?,?,?,?,?)",
            (kind, project_id, task_id, attempt_id, utc_now(), canonical_json(payload)),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise Fault("NOT_FOUND", "Unknown project")
        return row

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict:
        return {"project_id": row["id"], "root": row["root"], "config": _json(row["config"]),
                "enabled": bool(row["enabled"]), "plan": _json(row["plan"])}

    @staticmethod
    def _task_row(conn: sqlite3.Connection, project_id: str, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tasks WHERE project_id=? AND task_id=?", (project_id, task_id)).fetchone()
        if row is None:
            raise Fault("NOT_FOUND", "Unknown task")
        return row

    @staticmethod
    def _attempt_row(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise Fault("NOT_FOUND", "Unknown attempt")
        return row

    def _attempt_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        task = self._task_row(conn, row["project_id"], row["task_id"])
        project = self._project_row(conn, row["project_id"])
        outbox = conn.execute("SELECT state FROM outbox WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
        terminal = _json(row["terminal_observation"]) or {}
        return {"attempt_id": row["attempt_id"], "project_id": row["project_id"],
                "task_id": row["task_id"], "generation": row["generation"], "state": row["state"],
                "task": _json(task["definition"]), "project": self._project_dict(project),
                "launch_key": row["launch_key"], "launch_state": outbox["state"] if outbox else None,
                "identity": _json(row["identity"]),
                "exit_code": row["exit_code"], "cancel_requested": bool(row["cancel_requested"]),
                "outcome": terminal.get("payload") if terminal.get("kind") == "exited" else None,
                "dependency_snapshot": _json(row["dependency_snapshot"])}

    def _current_plan(self, conn: sqlite3.Connection, project_id: str) -> dict:
        plan = _json(self._project_row(conn, project_id)["plan"])
        if plan is None:
            raise Fault("DEPENDENCY_BLOCKED", "No approved plan has been registered")
        return plan

    def _assert_current(self, conn: sqlite3.Connection, task: sqlite3.Row) -> dict:
        definition = _json(task["definition"])
        if definition["plan"] != _plan_binding(self._current_plan(conn, task["project_id"])):
            raise Fault("CONTRACT_STALE", "Task belongs to an obsolete plan revision")
        return definition

    def enable_project(self, project_id: str, root: str, config: dict,
                       principal: Principal, request_id: str) -> dict:
        _identifier(project_id, "project_id")
        if not isinstance(config, dict):
            raise Fault("INVALID_REQUEST", "Project config must be an object")
        config = _json(canonical_json(config))
        _integer(config.get("project_limit", 4), "project_limit")
        config.setdefault("project_limit", 4)
        if config.get("profile") not in {"controlled_executor", "cooperative_native"}:
            raise Fault("INVALID_REQUEST", "Choose an explicit execution profile")
        if not isinstance(config.get("review_authority"), str) or not config["review_authority"]:
            raise Fault("INVALID_REQUEST", "An independent review authority is required")
        real_root = Path(root).expanduser().resolve()
        if not real_root.is_dir():
            raise Fault("INVALID_REQUEST", "Project root must be an existing directory")
        state_root = self.path.parent
        if real_root == state_root or real_root in state_root.parents or state_root in real_root.parents:
            raise Fault("SCOPE_CONFLICT", "Controller state and project must not contain one another")

        def change(conn: sqlite3.Connection) -> dict:
            existing = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if existing:
                if existing["root"] != str(real_root):
                    raise Fault("REVISION_CONFLICT", "Project root identity is immutable")
                if _json(existing["config"]) != config:
                    if conn.execute("SELECT 1 FROM publications WHERE project_id=?", (project_id,)).fetchone():
                        raise Fault("INSTANCE_UNCERTAIN", "Resolve the pending publication before changing project policy")
                    if existing["enabled"] or conn.execute(
                            f"SELECT 1 FROM attempts WHERE project_id=? AND state IN {ACTIVE_SQL}", (project_id,)).fetchone():
                        raise Fault("REVISION_CONFLICT", "Disable admission and confirm all exits before changing project policy")
                    if _json(existing["config"])["review_authority"] != config["review_authority"]:
                        self._revoke_history(conn, project_id)
                        conn.execute("DELETE FROM gates WHERE project_id=? AND gate='final_accepted'", (project_id,))
                        conn.execute("UPDATE tasks SET state='AWAITING_REVIEW' WHERE project_id=? AND state='ACCEPTED'", (project_id,))
                    conn.execute("UPDATE projects SET config=? WHERE id=?", (canonical_json(config), project_id))
                conn.execute("UPDATE projects SET enabled=1 WHERE id=?", (project_id,))
            else:
                # Two aliases for the same filesystem tree cannot create two independent ownership domains.
                for other in conn.execute("SELECT root FROM projects"):
                    other_root = Path(other["root"])
                    if real_root == other_root or real_root in other_root.parents or other_root in real_root.parents:
                        raise Fault("SCOPE_CONFLICT", "Project roots may not overlap in one resource domain")
                conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)",
                             (project_id, str(real_root), canonical_json(config), 1, None, utc_now()))
            self._event(conn, "project_enabled", project_id, profile=config["profile"])
            return self._project_dict(self._project_row(conn, project_id))
        return self._mutate("project.enable", {"project_id": project_id, "root": str(real_root), "config": config},
                            principal, request_id, {"operator"}, change)

    def disable_project(self, project_id: str, principal: Principal, request_id: str) -> dict:
        def change(conn: sqlite3.Connection) -> dict:
            self._project_row(conn, project_id)
            conn.execute("UPDATE projects SET enabled=0 WHERE id=?", (project_id,))
            self._event(conn, "project_disabled", project_id)
            return self._project_dict(self._project_row(conn, project_id))
        return self._mutate("project.disable", {"project_id": project_id}, principal, request_id, {"operator"}, change)

    def register_plan(self, project_id: str, plan: dict, principal: Principal, request_id: str) -> dict:
        if not isinstance(plan, dict):
            raise Fault("INVALID_REQUEST", "Plan must be an object")
        plan = _json(canonical_json(plan))
        _identifier(plan.get("id"), "plan id")
        _integer(plan.get("revision"), "plan revision", maximum=2_147_483_647)
        _artifact(plan.get("scope_digest"))
        if not isinstance(plan.get("approved_scope"), list):
            raise Fault("INVALID_REQUEST", "Plan must identify the exact approved scope")
        if plan["scope_digest"] != digest(plan["approved_scope"]):
            raise Fault("REVISION_CONFLICT", "Plan scope digest does not match its declared approved_scope")
        if not isinstance(plan.get("approval_ref"), str) or not 1 <= len(plan["approval_ref"]) <= 8192:
            raise Fault("UNAUTHORIZED", "A concrete operator-provided approval reference is required")
        contracts = plan.setdefault("contracts", [])
        if not isinstance(contracts, list):
            raise Fault("INVALID_REQUEST", "Plan contracts must be a list")
        seen = set()
        for contract in contracts:
            if not isinstance(contract, dict) or set(contract) != {"id", "version", "digest"}:
                raise Fault("INVALID_REQUEST", "Malformed approved contract")
            _identifier(contract["id"], "contract id")
            _artifact(contract["digest"])
            if not isinstance(contract["version"], str) or not contract["version"] or contract["id"] in seen:
                raise Fault("INVALID_REQUEST", "Contract ids must be unique and versions nonempty")
            seen.add(contract["id"])

        def change(conn: sqlite3.Connection) -> dict:
            project = self._project_row(conn, project_id)
            normalize_scope(project["root"], plan["approved_scope"])
            old = _json(project["plan"])
            if old == plan:
                return plan
            if conn.execute("SELECT 1 FROM publications WHERE project_id=?", (project_id,)).fetchone():
                raise Fault("INSTANCE_UNCERTAIN", "Resolve the pending publication before revising the plan")
            if old and (old["id"] != plan["id"] or plan["revision"] <= old["revision"]):
                raise Fault("REVISION_CONFLICT", "Plan revisions must advance within the same approved plan id")
            if conn.execute(f"SELECT 1 FROM attempts WHERE project_id=? AND state IN {ACTIVE_SQL}", (project_id,)).fetchone():
                raise Fault("INSTANCE_UNCERTAIN", "Finish or confirm cancellation of all old attempts before revising the plan")
            conn.execute("UPDATE projects SET plan=? WHERE id=?", (canonical_json(plan), project_id))
            if old:
                self._revoke_history(conn, project_id)
                conn.execute("UPDATE tasks SET state='BLOCKED',reason='CONTRACT_STALE',candidate=NULL WHERE project_id=? AND state!='CANCELLED'", (project_id,))
                conn.execute("DELETE FROM gates WHERE project_id=?", (project_id,))
            self._event(conn, "plan_registered", project_id, plan=_plan_binding(plan), approval_ref=plan["approval_ref"])
            return plan
        return self._mutate("plan.register", {"project_id": project_id, "plan": plan}, principal, request_id,
                            {"operator", "planner"}, change)

    @staticmethod
    def _within_scope(item: dict, approved: list[dict]) -> bool:
        for parent in approved:
            if item["key"] == parent["key"]:
                if item["kind"] == "file" or parent["kind"] == "directory":
                    return True
            if parent["kind"] == "directory" and item["key"].startswith(parent["key"].rstrip("/") + "/"):
                return True
        return False

    @staticmethod
    def _assert_dag(definitions: dict[str, dict]) -> None:
        # Kahn's algorithm does not recurse through a planner-supplied graph.
        incoming = {task_id: 0 for task_id in definitions}
        dependents = {task_id: [] for task_id in definitions}
        for task_id, definition in definitions.items():
            for dependency in definition["depends_on"]:
                source = dependency["task_id"]
                if source in definitions:
                    incoming[task_id] += 1
                    dependents[source].append(task_id)
        ready = [task_id for task_id, degree in incoming.items() if degree == 0]
        count = 0
        while ready:
            source = ready.pop()
            count += 1
            for child in dependents[source]:
                incoming[child] -= 1
                if incoming[child] == 0:
                    ready.append(child)
        if count != len(definitions):
            raise Fault("DEPENDENCY_CYCLE", "Task dependency graph contains a cycle")

    def register_task(self, project_id: str, task: dict, principal: Principal, request_id: str) -> dict:
        definition = validate_task(task)
        def change(conn: sqlite3.Connection) -> dict:
            project = self._project_row(conn, project_id)
            plan = self._current_plan(conn, project_id)
            if definition["plan"] != _plan_binding(plan):
                raise Fault("CONTRACT_STALE", "Task must bind the current approved plan exactly")
            approved_contracts = {x["id"]: x for x in plan["contracts"]}
            for contract in definition["contract_versions"]:
                if approved_contracts.get(contract["id"]) != contract:
                    raise Fault("CONTRACT_STALE", "Task uses an unapproved or stale contract version")
            scopes = normalize_scope(project["root"], definition["write_scope"])
            if len({item["key"] for item in scopes}) != len(scopes):
                raise Fault("INVALID_SCOPE", "Duplicate write-scope ownership entries")
            approved_scope = normalize_scope(project["root"], plan["approved_scope"])
            if not all(self._within_scope(item, approved_scope) for item in scopes):
                raise Fault("SCOPE_CONFLICT", "Task write scope exceeds the approved plan")
            existing = conn.execute("SELECT * FROM tasks WHERE project_id=? AND task_id=?",
                                    (project_id, definition["task_id"])).fetchone()
            if existing:
                if _json(existing["definition"]) != definition:
                    raise Fault("REVISION_CONFLICT", "Task identities are immutable; register a new task id")
                return self._task_dict(conn, existing)
            definitions = {row["task_id"]: _json(row["definition"]) for row in conn.execute(
                "SELECT task_id,definition FROM tasks WHERE project_id=?", (project_id,))}
            definitions[definition["task_id"]] = definition
            self._assert_dag(definitions)
            seq = self._event(conn, "task_registered", project_id, definition["task_id"], plan=definition["plan"])
            conn.execute("INSERT INTO tasks(project_id,task_id,definition,scopes,state,created_seq) VALUES(?,?,?,?,?,?)",
                         (project_id, definition["task_id"], canonical_json(definition), canonical_json(scopes), "PLANNED", seq))
            return self._task_dict(conn, self._task_row(conn, project_id, definition["task_id"]))
        return self._mutate("task.register", {"project_id": project_id, "task": definition}, principal, request_id,
                            {"operator", "planner", "coordinator"}, change)

    @staticmethod
    def _history_epoch(conn: sqlite3.Connection, project_id: str, task_id: str) -> int:
        row = conn.execute("SELECT epoch FROM gate_history_epochs WHERE project_id=? AND task_id=?",
                           (project_id, task_id)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _revoke_history(conn: sqlite3.Connection, project_id: str, task_id: str | None = None) -> None:
        """Fence prior immutable proof records; their content is never rewritten."""
        rows = conn.execute("SELECT task_id FROM tasks WHERE project_id=?" + (" AND task_id=?" if task_id else ""),
                            (project_id, task_id) if task_id else (project_id,)).fetchall()
        for row in rows:
            conn.execute("INSERT INTO gate_history_epochs VALUES(?,?,1) ON CONFLICT(project_id,task_id) DO UPDATE SET epoch=epoch+1",
                         (project_id, row["task_id"]))

    def _append_gate_history(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        conn.execute("INSERT OR IGNORE INTO gate_history VALUES(?,?,?,?,?,?,?,?,?)",
                     (row["created_seq"], row["project_id"], row["task_id"], row["gate"],
                      row["artifact_digest"], row["binding"], row["dependencies"], row["principal_id"],
                      self._history_epoch(conn, row["project_id"], row["task_id"])))

    def _valid_gate(self, conn: sqlite3.Connection, project_id: str, task_id: str, gate: str,
                    visited: set[tuple[str, str, str | None]] | None = None,
                    artifact_digest: str | None = None) -> sqlite3.Row | None:
        key = (task_id, gate, artifact_digest)
        visited = set() if visited is None else set(visited)
        if key in visited or len(visited) > 128:
            return None
        visited.add(key)
        row = conn.execute("SELECT * FROM gates WHERE project_id=? AND task_id=? AND gate=?", (project_id, task_id, gate)).fetchone()
        historical = False
        if artifact_digest is not None and (row is None or row["artifact_digest"] != artifact_digest):
            # Only immutable combined deliveries and their exact reviews have
            # historical semantics. Contract/implementation changes still make
            # dependent evidence stale; they cannot be bypassed with an old pin.
            if gate not in {"integrated_verified", "final_accepted"}:
                return None
            row = conn.execute(
                "SELECT * FROM gate_history WHERE project_id=? AND task_id=? AND gate=? AND artifact_digest=? AND epoch=? ORDER BY created_seq DESC LIMIT 1",
                (project_id, task_id, gate, artifact_digest, self._history_epoch(conn, project_id, task_id))).fetchone()
            historical = True
        if row is None:
            return None
        task = self._task_row(conn, project_id, task_id)
        if task["cancel_requested"] or (historical and task["state"] == "BLOCKED"):
            return None
        try:
            definition = self._assert_current(conn, task)
        except Fault:
            return None
        binding = _json(row["binding"])
        if binding.get("plan") != definition["plan"]:
            return None
        candidate = _json(task["candidate"])
        if gate == "implementation_verified":
            if not candidate or candidate["artifact_digest"] != row["artifact_digest"]:
                return None
        elif gate == "integrated_verified":
            if not candidate or binding.get("candidate_digest") != candidate["artifact_digest"]:
                return None
            implementation = self._valid_gate(conn, project_id, task_id, "implementation_verified", visited)
            if not implementation or implementation["artifact_digest"] != candidate["artifact_digest"]:
                return None
        elif gate == "final_accepted":
            authority = _json(self._project_row(conn, project_id)["config"])["review_authority"]
            if row["principal_id"] != authority or binding.get("decision", "approve") != "approve":
                return None
            integrated = self._valid_gate(conn, project_id, task_id, "integrated_verified", visited,
                                          artifact_digest=row["artifact_digest"])
            if not integrated or integrated["artifact_digest"] != row["artifact_digest"]:
                return None
        for dep in _json(row["dependencies"]):
            # These are authenticated consumption snapshots, not new admission
            # requests. A later combined delivery cannot change old inputs.
            pinned = dep["artifact_digest"] if dep["gate"] in {"integrated_verified", "final_accepted"} else None
            current = self._valid_gate(conn, project_id, dep["task_id"], dep["gate"], visited,
                                       artifact_digest=pinned)
            if not current or current["artifact_digest"] != dep["artifact_digest"]:
                return None
        return row

    def _dependency_snapshot(self, conn: sqlite3.Connection, task: sqlite3.Row) -> list[dict]:
        """Resolve fresh admission: unpinned edges always select current proof."""
        definition = self._assert_current(conn, task)
        result = []
        for dependency in definition["depends_on"]:
            gate = self._valid_gate(conn, task["project_id"], dependency["task_id"], dependency["gate"],
                                    artifact_digest=dependency.get("artifact_digest"))
            if gate is None or (dependency.get("artifact_digest") and dependency["artifact_digest"] != gate["artifact_digest"]):
                raise Fault("DEPENDENCY_BLOCKED", f"Dependency {dependency['task_id']}:{dependency['gate']} is not current or validly pinned")
            result.append({"task_id": dependency["task_id"], "gate": dependency["gate"], "artifact_digest": gate["artifact_digest"]})
        return result

    def _attempt_dependencies(self, conn: sqlite3.Connection, task: sqlite3.Row, attempt: sqlite3.Row) -> list[dict]:
        """Validate the exact inputs consumed by an already reserved attempt."""
        definition = self._assert_current(conn, task)
        snapshot = _json(attempt["dependency_snapshot"])
        declared = {(entry["task_id"], entry["gate"]): entry for entry in definition["depends_on"]}
        if {(entry["task_id"], entry["gate"]) for entry in snapshot} != set(declared):
            raise Fault("CONTRACT_STALE", "Attempt consumption snapshot does not match the declared task dependencies")
        for entry in snapshot:
            expected = declared[(entry["task_id"], entry["gate"])].get("artifact_digest")
            if expected is not None and expected != entry["artifact_digest"]:
                raise Fault("CONTRACT_STALE", "Attempt used a different dependency than the explicit pin")
            pin = entry["artifact_digest"] if entry["gate"] in {"integrated_verified", "final_accepted"} else None
            proof = self._valid_gate(conn, task["project_id"], entry["task_id"], entry["gate"], artifact_digest=pin)
            if not proof or proof["artifact_digest"] != entry["artifact_digest"]:
                raise Fault("CONTRACT_STALE", "An input consumed by this attempt has been revoked or changed")
        return snapshot

    def _eligible(self, conn: sqlite3.Connection, task: sqlite3.Row,
                  now: float | None = None) -> tuple[bool, str | None]:
        project = self._project_row(conn, task["project_id"])
        if not project["enabled"]:
            return False, "PROJECT_DISABLED"
        if task["cancel_requested"] or task["state"] == "CANCELLED":
            return False, "CANCELLED"
        if task["state"] not in {"PLANNED", "READY", "REWORK"}:
            return False, task["reason"] or task["state"]
        if task["not_before"] > (time.time() if now is None else now):
            return False, "RETRY_BACKOFF"
        try:
            self._dependency_snapshot(conn, task)
        except Fault as exc:
            return False, exc.code
        return True, None

    def _task_dict(self, conn: sqlite3.Connection, row: sqlite3.Row,
                   now: float | None = None) -> dict:
        eligible, reason = self._eligible(conn, row, now)
        gates = {}
        for name in GATES:
            valid = self._valid_gate(conn, row["project_id"], row["task_id"], name)
            if valid:
                gates[name] = {"artifact_digest": valid["artifact_digest"], "binding": _json(valid["binding"]),
                               "principal_id": valid["principal_id"]}
        state = "READY" if eligible else row["state"]
        required_gate = {"TECHNICALLY_VERIFIED": "implementation_verified",
                         "AWAITING_REVIEW": "integrated_verified", "ACCEPTED": "final_accepted"}.get(state)
        if required_gate and required_gate not in gates:
            state, reason = "BLOCKED", "CONTRACT_STALE"
        return {"project_id": row["project_id"], "task_id": row["task_id"],
                "task": _json(row["definition"]), "state": state,
                "blocked_reason": reason, "created_seq": row["created_seq"],
                "generation": row["generation"], "not_before": row["not_before"],
                "cancel_requested": bool(row["cancel_requested"]), "candidate": _json(row["candidate"]),
                "latest_attempt": row["latest_attempt"], "gates": gates}

    def _ordered_tasks(self, conn: sqlite3.Connection, project_id: str | None,
                       now: float) -> list[tuple[sqlite3.Row, dict, bool]]:
        rows = conn.execute(
            "SELECT t.*,e.occurred_at AS registered_at,e.seq AS registration_seq,e.type AS registration_type,"
            "e.project_id AS registration_project_id,e.task_id AS registration_task_id "
            "FROM tasks t LEFT JOIN events e ON e.seq=t.created_seq" +
            (" WHERE t.project_id=?" if project_id is not None else ""),
            (project_id,) if project_id is not None else ()).fetchall()
        ranked = []
        for row in rows:
            if (row["registration_seq"] != row["created_seq"] or
                    row["registration_type"] != "task_registered" or
                    row["registration_project_id"] != row["project_id"] or
                    row["registration_task_id"] != row["task_id"] or
                    not isinstance(row["registered_at"], str)):
                raise Fault("QUEUE_INCONSISTENT", "Task registration history is missing or does not match its task")
            try:
                rank = scheduling.rank(_json(row["definition"]), row["registered_at"], now)
            except (ValueError, TypeError, OverflowError):
                raise Fault("QUEUE_INCONSISTENT", "Task registration history cannot be ranked at a valid timestamp") from None
            ranked.append((row, rank, self._eligible(conn, row, now)[0]))
        return sorted(ranked, key=lambda item: (
            not item[2], *scheduling.order_key(item[1]["effective_rank"], item[0]["created_seq"],
                                             item[0]["project_id"], item[0]["task_id"])))

    def ready_tasks(self, project_id: str | None = None) -> list[dict]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            now = time.time()
            return [self._task_dict(conn, row, now)
                    for row, _rank, eligible in self._ordered_tasks(conn, project_id, now) if eligible]

    @staticmethod
    def _leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT l.*,a.project_id,a.task_id FROM leases l JOIN attempts a ON a.attempt_id=l.attempt_id "
            "ORDER BY a.created_seq,l.kind,l.resource_key").fetchall()

    def _admission(self, conn: sqlite3.Connection, task: sqlite3.Row, now: float,
                   *, active: list[sqlite3.Row] | None = None,
                   leases: list[sqlite3.Row] | None = None,
                   limit: int | None = None, all_blockers: bool = False) -> _Admission:
        """Check the same current inputs for an advisory preview and reservation.

        First-fault order preserves the reservation API. Summaries omit raw
        reasons, scopes and resource names, and coalesce conflicts per owner and
        kind. This method never creates a lease or modifies ledger evidence.
        """
        eligible, reason = self._eligible(conn, task, now)
        result = _Admission(eligible)
        if not eligible:
            code = reason if reason in {
                "PROJECT_DISABLED", "CANCELLED", "RETRY_BACKOFF", "RETRY_EXHAUSTED",
                "CONTRACT_STALE", "DEPENDENCY_BLOCKED", "REVIEW_REJECTED", "REWORK_REQUIRED",
                "IMPLEMENTING", "SUBMITTED", "TECHNICALLY_VERIFIED", "AWAITING_REVIEW", "ACCEPTED",
            } else "TASK_BLOCKED"
            details = {"retry_after_seconds": max(0, task["not_before"] - now)} if reason == "RETRY_BACKOFF" else {}
            result.block(Fault(reason if reason in {"CONTRACT_STALE", "CANCELLED"} else "DEPENDENCY_BLOCKED",
                               f"Task is not eligible: {reason}"), code, "eligibility", **details)
            return result
        project = self._project_row(conn, task["project_id"])
        active = (conn.execute(f"SELECT * FROM attempts WHERE state IN {ACTIVE_SQL}").fetchall()
                  if active is None else active)
        limit = (int(conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()[0])
                 if limit is None else limit)
        project_limit = _json(project["config"])["project_limit"]
        if len(active) >= limit:
            result.block(Fault("CAPACITY_FULL", "Managed-worker capacity is full"), "CAPACITY_FULL", "global_capacity")
            if not all_blockers:
                return result
        if sum(row["project_id"] == task["project_id"] for row in active) >= project_limit:
            result.block(Fault("CAPACITY_FULL", "Managed-worker capacity is full"), "CAPACITY_FULL", "project_capacity")
            if not all_blockers:
                return result
        definition = _json(task["definition"])
        try:
            # Registration is not a perpetual path capability. Filesystem state
            # is sampled here and is resolved again at each actual reservation.
            result.scopes = normalize_scope(project["root"], definition["write_scope"])
            if result.scopes != _json(task["scopes"]):
                result.block(Fault("SCOPE_CONFLICT", "Filesystem ownership changed after registration"),
                             "SCOPE_CONFLICT", "scope_changed")
        except Fault as exc:
            result.block(exc, exc.code, "scope_invalid")
        if result.fault is not None and not all_blockers:
            return result
        result.resources = set(definition["exclusive_resources"])
        conflicts = set()
        for lease in self._leases(conn) if leases is None else leases:
            kind = None
            if lease["kind"] == "resource" and lease["resource_key"] in result.resources:
                kind = "exclusive_resource"
            elif lease["kind"] == "write_scope" and scopes_overlap(result.scopes, [_json(lease["value"])]):
                kind = "write_scope"
            if kind is not None and (lease["attempt_id"], kind) not in conflicts:
                conflicts.add((lease["attempt_id"], kind))
                message = ("An exclusive resource is already leased" if kind == "exclusive_resource"
                           else "Write scopes overlap an active lease")
                details = {"resource": lease["resource_key"]} if kind == "exclusive_resource" else None
                result.block(Fault("SCOPE_CONFLICT", message, details), "SCOPE_CONFLICT", kind,
                             owner={"attempt_id": lease["attempt_id"], "project_id": lease["project_id"],
                                    "task_id": lease["task_id"]})
                if not all_blockers:
                    return result
        result.snapshot = self._dependency_snapshot(conn, task)
        result.generation = task["generation"] + 1
        if result.generation > definition["retry"]["max_retries"] + 1:
            result.block(Fault("RETRY_EXHAUSTED", "Task retry budget is exhausted"), "RETRY_EXHAUSTED", "retry_budget")
        return result

    def queue(self, project_id: str | None = None, limit: int = 100) -> dict:
        """Bounded advisory diagnostics at one ledger snapshot and one instant.

        Entries cover registered tasks, with eligible tasks first. Registration
        age includes dependency/backoff waits; completed or active rows remain
        visible after eligible rows. Filesystem checks are observations, not
        leases: reserve() rechecks admission atomically before assigning capacity.
        Population budgets are checked before materializing diagnostic rows;
        exceeding one fails explicitly, never returning a partially ranked queue.
        """
        _integer(limit, "queue limit", maximum=200)
        if project_id is not None:
            _identifier(project_id, "project_id")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            cursor = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            now = time.time()
            where = " WHERE project_id=?" if project_id is not None else ""
            project_where = " WHERE id=?" if project_id is not None else ""
            args = (project_id,) if project_id is not None else ()
            if project_id is not None and conn.execute("SELECT 1 FROM projects WHERE id=?", args).fetchone() is None:
                raise Fault("NOT_FOUND", "Unknown project")
            self._queue_budget(conn, "SELECT 1 FROM tasks" + where, args, _QUEUE_MAX_TASKS, "tasks")
            self._queue_budget(conn, "SELECT 1 FROM projects" + project_where, args, _QUEUE_MAX_PROJECTS, "projects")
            self._queue_budget(conn, f"SELECT 1 FROM attempts WHERE state IN {ACTIVE_SQL}", (),
                               _QUEUE_MAX_ACTIVE_ATTEMPTS, "active_attempts")
            self._queue_budget(conn, "SELECT 1 FROM leases", (), _QUEUE_MAX_LEASES, "leases")
            projects = conn.execute("SELECT * FROM projects" + (" WHERE id=?" if project_id is not None else ""),
                                    (project_id,) if project_id is not None else ()).fetchall()
            active = conn.execute(f"SELECT * FROM attempts WHERE state IN {ACTIVE_SQL}").fetchall()
            domain_limit = int(conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()[0])
            capacity = {"used": len(active), "limit": domain_limit,
                        "available": max(0, domain_limit - len(active)),
                        "uncertain": sum(row["state"] == "LOST" for row in active)}
            per_project = {}
            for project in projects:
                used = sum(row["project_id"] == project["id"] for row in active)
                project_limit = _json(project["config"])["project_limit"]
                per_project[project["id"]] = {
                    "used": used, "limit": project_limit,
                    "available": max(0, min(domain_limit - len(active), project_limit - used))}
            leases = self._leases(conn)
            ranked = self._ordered_tasks(conn, project_id, now)
            entries = []
            for row, rank, _eligible in ranked[:limit]:
                decision = self._admission(conn, row, now, active=active, leases=leases,
                                           limit=domain_limit, all_blockers=True)
                entries.append({"project_id": row["project_id"], "task_id": row["task_id"],
                                "role": _json(row["definition"])["role"],
                                "state": "READY" if decision.eligible else row["state"],
                                "created_seq": row["created_seq"], **rank,
                                "eligible": decision.eligible, "dispatchable": decision.fault is None,
                                "blockers": decision.blockers, "blocker_count": decision.blocker_count,
                                "blockers_truncated": decision.blocker_count > len(decision.blockers),
                                "project_capacity": per_project[row["project_id"]]})
            return {"schema_version": 1, "advisory": True, "event_cursor": cursor, "observed_at": now,
                    "policy": scheduling.policy(), "capacity": capacity, "entries": entries,
                    "total": len(ranked), "truncated": len(ranked) > len(entries)}

    @staticmethod
    def _queue_budget(conn: sqlite3.Connection, select: str, args: tuple,
                      maximum: int, population: str) -> None:
        # Count at most budget+1 indexed rows. An over-budget count is a lower
        # bound, not a reason to materialize or rank a partial task population.
        count = conn.execute("SELECT COUNT(*) FROM (" + select + " LIMIT ?)", (*args, maximum + 1)).fetchone()[0]
        if count > maximum:
            raise Fault("QUEUE_LIMIT", "Queue diagnostics exceed the complete-snapshot budget; select one project "
                        "to reduce task/project scope. Active attempts and leases use domain-wide budgets; "
                        "a smaller result limit does not reduce snapshot work.",
                        {"population": population, "limit": maximum, "at_least": count})

    def reserve(self, project_id: str, task_id: str, principal: Principal, request_id: str) -> dict:
        def change(conn: sqlite3.Connection) -> dict:
            task = self._task_row(conn, project_id, task_id)
            decision = self._admission(conn, task, time.time())
            if decision.fault is not None:
                raise decision.fault
            scopes, requested_resources = decision.scopes, decision.resources
            snapshot, generation = decision.snapshot, decision.generation
            attempt_id = uuid.uuid4().hex
            launch_key = f"launch:{attempt_id}:{generation}"
            seq = self._event(conn, "run_reserved", project_id, task_id, attempt_id,
                              generation=generation, scopes=scopes, resources=sorted(requested_resources))
            conn.execute("INSERT INTO attempts(attempt_id,project_id,task_id,generation,state,launch_key,dependency_snapshot,created_seq) VALUES(?,?,?,?,?,?,?,?)",
                         (attempt_id, project_id, task_id, generation, "RESERVED", launch_key, canonical_json(snapshot), seq))
            conn.execute("INSERT INTO outbox VALUES(?,?,?)", (attempt_id, launch_key, "PENDING"))
            for item in scopes:
                conn.execute("INSERT INTO leases VALUES(?,?,?,?)", (attempt_id, "write_scope", item["key"], canonical_json(item)))
            for resource in requested_resources:
                conn.execute("INSERT INTO leases VALUES(?,?,?,?)", (attempt_id, "resource", resource, canonical_json(resource)))
            self._revoke_history(conn, project_id, task_id)
            conn.execute("UPDATE tasks SET state='IMPLEMENTING',reason=NULL,generation=?,latest_attempt=?,candidate=NULL WHERE project_id=? AND task_id=?",
                         (generation, attempt_id, project_id, task_id))
            conn.execute("DELETE FROM gates WHERE project_id=? AND task_id=? AND gate!='contract_ready'", (project_id, task_id))
            return self._attempt_dict(conn, self._attempt_row(conn, attempt_id))
        return self._mutate("dispatch.request", {"project_id": project_id, "task_id": task_id}, principal, request_id,
                            {"operator", "planner", "coordinator"}, change)

    def _generation(self, conn: sqlite3.Connection, attempt_id: str, generation: int) -> sqlite3.Row:
        row = self._attempt_row(conn, attempt_id)
        task = self._task_row(conn, row["project_id"], row["task_id"])
        if type(generation) is not int or generation != row["generation"] or generation != task["generation"] or task["latest_attempt"] != attempt_id:
            raise Fault("REVISION_CONFLICT", "Observation belongs to a stale attempt generation")
        return row

    def claim_launch(self, attempt_id: str, generation: int, principal: Principal, request_id: str) -> dict:
        def change(conn: sqlite3.Connection) -> dict:
            row = self._generation(conn, attempt_id, generation)
            if row["state"] != "RESERVED" or row["cancel_requested"]:
                raise Fault("INSTANCE_UNCERTAIN", "Launch was already claimed or cancelled; reconcile rather than starting another process")
            project = self._project_row(conn, row["project_id"])
            if not project["enabled"]:
                raise Fault("DEPENDENCY_BLOCKED", "Project admission is disabled; reserved launch remains paused")
            self._assert_current(conn, self._task_row(conn, row["project_id"], row["task_id"]))
            conn.execute("UPDATE attempts SET state='STARTING' WHERE attempt_id=?", (attempt_id,))
            conn.execute("UPDATE outbox SET state='CLAIMED' WHERE attempt_id=?", (attempt_id,))
            self._event(conn, "launch_claimed", row["project_id"], row["task_id"], attempt_id, generation=generation)
            return self._attempt_dict(conn, self._attempt_row(conn, attempt_id))
        return self._mutate("launch.claim", {"attempt_id": attempt_id, "generation": generation}, principal, request_id,
                            {"executor"}, change)

    def launches(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return [self._attempt_dict(conn, row) for row in conn.execute(
                "SELECT a.* FROM attempts a JOIN outbox o ON o.attempt_id=a.attempt_id WHERE a.state='RESERVED' AND o.state='PENDING' ORDER BY a.created_seq")]

    def observe(self, attempt_id: str, generation: int, kind: str, payload: dict,
                principal: Principal, request_id: str) -> dict:
        if kind not in {"started", "lost", "exited", "start_failed"} or not isinstance(payload, dict):
            raise Fault("INVALID_REQUEST", "Unknown lifecycle observation")
        payload = _json(canonical_json(payload))
        def change(conn: sqlite3.Connection) -> dict:
            row = self._generation(conn, attempt_id, generation)
            project_id, task_id = row["project_id"], row["task_id"]
            current_identity = _json(row["identity"])
            if row["state"] == "RELEASED":
                if _json(row["terminal_observation"]) == {"kind": kind, "payload": payload}:
                    return self._attempt_dict(conn, row)
                raise Fault("REVISION_CONFLICT", "A terminal observation cannot be changed or release capacity twice")
            if kind == "started":
                identity = payload.get("identity")
                if not isinstance(identity, dict) or not identity:
                    raise Fault("UNKNOWN_IDENTITY", "Executor must attest a nonempty process identity")
                if row["state"] not in {"STARTING", "LOST", "STOPPING", "RUNNING"}:
                    raise Fault("REVISION_CONFLICT", "A start acknowledgement requires a claimed launch")
                if current_identity is not None and current_identity != identity:
                    raise Fault("UNKNOWN_IDENTITY", "Launch identity cannot change")
                state = "STOPPING" if row["cancel_requested"] else "RUNNING"
                conn.execute("UPDATE attempts SET state=?,identity=? WHERE attempt_id=?", (state, canonical_json(identity), attempt_id))
                self._event(conn, "run_start_ack", project_id, task_id, attempt_id, generation=generation, identity=identity)
            elif kind == "lost":
                if not isinstance(payload.get("reason"), str) or not payload["reason"]:
                    raise Fault("INVALID_REQUEST", "Uncertain instances require a reason")
                conn.execute("UPDATE attempts SET state='LOST' WHERE attempt_id=?", (attempt_id,))
                self._event(conn, "run_lost", project_id, task_id, attempt_id, generation=generation, reason=payload["reason"])
            else:
                if kind == "start_failed":
                    if payload.get("no_process_created") is not True or current_identity is not None or row["state"] not in {"RESERVED", "STARTING", "STOPPING", "LOST"}:
                        raise Fault("INSTANCE_UNCERTAIN", "A start failure may release only an attested never-created process")
                    if not isinstance(payload.get("reason"), str) or not payload["reason"]:
                        raise Fault("INVALID_REQUEST", "Start failures require a reason")
                    exit_code = None
                else:
                    if payload.get("process_tree_terminated") is not True:
                        raise Fault("INSTANCE_UNCERTAIN", "Process-tree termination is unconfirmed; capacity remains reserved")
                    if current_identity is None or payload.get("identity") != current_identity:
                        raise Fault("UNKNOWN_IDENTITY", "Exit identity does not match the acknowledged launch")
                    exit_code = payload.get("exit_code")
                    if type(exit_code) is not int:
                        raise Fault("INVALID_REQUEST", "An observed integer exit status is required")
                    if any(key in payload and type(payload[key]) is not bool for key in ("timed_out", "cancelled")):
                        raise Fault("INVALID_REQUEST", "Timeout and cancellation observations must be booleans")
                task = self._task_row(conn, project_id, task_id)
                definition = _json(task["definition"])
                cancelled = bool(row["cancel_requested"] or task["cancel_requested"] or payload.get("cancelled"))
                if cancelled:
                    state, reason, not_before = "CANCELLED", "CANCELLED", 0
                elif kind == "exited" and exit_code == 0 and not payload.get("timed_out", False):
                    state, reason, not_before = "SUBMITTED", None, 0
                elif generation <= definition["retry"]["max_retries"]:
                    state, reason = "PLANNED", "RETRY_BACKOFF"
                    not_before = time.time() + definition["retry"]["backoff_ms"] / 1000
                else:
                    state, reason, not_before = "BLOCKED", "RETRY_EXHAUSTED", 0
                conn.execute("UPDATE attempts SET state='RELEASED',exit_code=?,terminal_observation=? WHERE attempt_id=?",
                             (exit_code, canonical_json({"kind": kind, "payload": payload}), attempt_id))
                conn.execute("UPDATE outbox SET state='DONE' WHERE attempt_id=?", (attempt_id,))
                conn.execute("DELETE FROM leases WHERE attempt_id=?", (attempt_id,))
                conn.execute("UPDATE tasks SET state=?,reason=?,not_before=? WHERE project_id=? AND task_id=?",
                             (state, reason, not_before, project_id, task_id))
                self._event(conn, "run_cancel_confirmed" if cancelled else ("run_exit_confirmed" if kind == "exited" else "run_start_failed"),
                            project_id, task_id, attempt_id, generation=generation, observation=payload)
                self._event(conn, "resource_released", project_id, task_id, attempt_id, generation=generation,
                            basis="process_tree_exit" if kind == "exited" else "no_process_created")
            return self._attempt_dict(conn, self._attempt_row(conn, attempt_id))
        return self._mutate("run.observe", {"attempt_id": attempt_id, "generation": generation, "kind": kind, "payload": payload},
                            principal, request_id, {"executor"}, change)

    def cancel(self, project_id: str, task_id: str | None, principal: Principal, request_id: str) -> dict:
        def change(conn: sqlite3.Connection) -> dict:
            self._project_row(conn, project_id)
            if task_id is not None:
                self._task_row(conn, project_id, task_id)
            else:
                conn.execute("UPDATE projects SET enabled=0 WHERE id=?", (project_id,))
            where, args = "project_id=?", [project_id]
            if task_id is not None:
                where += " AND task_id=?"
                args.append(task_id)
            self._revoke_history(conn, project_id, task_id)
            conn.execute(f"UPDATE tasks SET cancel_requested=1 WHERE {where}", args)
            conn.execute(f"UPDATE tasks SET state='CANCELLED',reason='CANCELLED' WHERE {where} AND state NOT IN ('IMPLEMENTING')", args)
            active = conn.execute(f"SELECT * FROM attempts WHERE {where} AND state IN {ACTIVE_SQL}", args).fetchall()
            for row in active:
                conn.execute("UPDATE attempts SET state='STOPPING',cancel_requested=1 WHERE attempt_id=?", (row["attempt_id"],))
                conn.execute("UPDATE outbox SET state='CANCELLED' WHERE attempt_id=? AND state='PENDING'", (row["attempt_id"],))
                self._event(conn, "run_cancel_requested", project_id, row["task_id"], row["attempt_id"], generation=row["generation"])
            self._event(conn, "cancel_requested", project_id, task_id, scope="task" if task_id else "project")
            return {"project_id": project_id, "task_id": task_id,
                    "attempts": [self._attempt_dict(conn, self._attempt_row(conn, row["attempt_id"])) for row in active]}
        return self._mutate("run.cancel", {"project_id": project_id, "task_id": task_id}, principal, request_id,
                            {"operator", "planner", "coordinator"}, change)

    def submit_candidate(self, attempt_id: str, generation: int, candidate: dict,
                         principal: Principal, request_id: str) -> dict:
        if not isinstance(candidate, dict):
            raise Fault("INVALID_REQUEST", "Candidate must be an object")
        candidate = _json(canonical_json(candidate))
        if not isinstance(candidate.get("commit"), str) or not _COMMIT.fullmatch(candidate["commit"]):
            raise Fault("INVALID_REQUEST", "Candidate must identify an exact commit")
        _artifact(candidate.get("artifact_digest"))
        def change(conn: sqlite3.Connection) -> dict:
            row = self._generation(conn, attempt_id, generation)
            if principal.role == "worker" and principal.id != attempt_id:
                raise Fault("UNAUTHORIZED", "Worker capability belongs to a different attempt")
            task = self._task_row(conn, row["project_id"], row["task_id"])
            definition = self._assert_current(conn, task)
            if task["state"] == "BLOCKED":
                raise Fault("DEPENDENCY_BLOCKED", "Blocked delivery requires an explicitly registered replacement task")
            outcome = (_json(row["terminal_observation"]) or {}).get("payload", {})
            if (row["state"] != "RELEASED" or row["exit_code"] != 0 or row["cancel_requested"] or task["cancel_requested"]
                    or outcome.get("timed_out") or outcome.get("cancelled")):
                raise Fault("INSTANCE_UNCERTAIN", "Candidate requires a confirmed successful current attempt")
            if candidate.get("plan") != definition["plan"]:
                raise Fault("CONTRACT_STALE", "Candidate must bind the exact task plan")
            self._attempt_dependencies(conn, task, row)
            paths = candidate.get("changed_paths", [])
            if not isinstance(paths, list) or not all(isinstance(path, str) and path_in_scope(path, definition["write_scope"]) for path in paths):
                raise Fault("SCOPE_CONFLICT", "Candidate changed paths exceed task scope")
            if _json(task["candidate"]) != candidate:
                self._revoke_history(conn, row["project_id"], row["task_id"])
                conn.execute("DELETE FROM gates WHERE project_id=? AND task_id=? AND gate!='contract_ready'", (row["project_id"], row["task_id"]))
                conn.execute("UPDATE tasks SET candidate=?,state='SUBMITTED',reason=NULL WHERE project_id=? AND task_id=?",
                             (canonical_json(candidate), row["project_id"], row["task_id"]))
            self._event(conn, "candidate_submitted", row["project_id"], row["task_id"], attempt_id,
                        generation=generation, artifact_digest=candidate["artifact_digest"], commit=candidate["commit"])
            return self._task_dict(conn, self._task_row(conn, row["project_id"], row["task_id"]))
        return self._mutate("evidence.submit", {"attempt_id": attempt_id, "generation": generation, "candidate": candidate},
                            principal, request_id, {"executor", "verifier", "worker"}, change)

    def record_gate(self, project_id: str, task_id: str, gate: str, artifact_digest: str,
                    binding: dict, principal: Principal, request_id: str) -> dict:
        roles = {"contract_ready": {"planner"}, "implementation_verified": {"verifier"},
                 "integrated_verified": {"integrator"}, "final_accepted": {"reviewer"}}
        if gate not in roles or not isinstance(binding, dict):
            raise Fault("INVALID_REQUEST", "Unknown gate or malformed binding")
        _artifact(artifact_digest)
        binding = _json(canonical_json(binding))
        def change(conn: sqlite3.Connection) -> dict:
            task = self._task_row(conn, project_id, task_id)
            definition = self._assert_current(conn, task)
            if task["cancel_requested"]:
                raise Fault("CANCELLED", "Cancelled tasks cannot advance quality gates")
            if task["state"] == "BLOCKED" and gate != "final_accepted":
                raise Fault("DEPENDENCY_BLOCKED", "Blocked delivery requires an explicitly registered replacement task")
            if binding.get("plan") != definition["plan"]:
                raise Fault("CONTRACT_STALE", "Gate must bind the exact current plan")
            dependencies = None
            candidate = _json(task["candidate"])
            if gate != "contract_ready":
                if not candidate:
                    raise Fault("DEPENDENCY_BLOCKED", "Technical gates require a submitted candidate")
                attempt = self._attempt_row(conn, task["latest_attempt"])
                if attempt["state"] != "RELEASED" or attempt["exit_code"] != 0:
                    raise Fault("CONTRACT_STALE", "Candidate attempt is no longer current")
                dependencies = self._attempt_dependencies(conn, task, attempt)
            else:
                dependencies = self._dependency_snapshot(conn, task)
            if gate == "implementation_verified":
                if candidate["artifact_digest"] != artifact_digest:
                    raise Fault("CONTRACT_STALE", "Verification must identify the exact submitted candidate")
                new_state = "TECHNICALLY_VERIFIED"
                invalidate = ("integrated_verified", "final_accepted")
            elif gate == "integrated_verified":
                implementation = self._valid_gate(conn, project_id, task_id, "implementation_verified")
                if binding.get("candidate_digest") != candidate["artifact_digest"] or implementation is None:
                    raise Fault("DEPENDENCY_BLOCKED", "Integration must bind the currently verified candidate")
                new_state = "AWAITING_REVIEW"
                invalidate = ("final_accepted",)
            elif gate == "final_accepted":
                project = self._project_row(conn, project_id)
                if principal.id != _json(project["config"])["review_authority"]:
                    raise Fault("UNAUTHORIZED", "Only the configured independent reviewer may accept delivery")
                integrated = self._valid_gate(conn, project_id, task_id, "integrated_verified")
                if not integrated or integrated["artifact_digest"] != artifact_digest:
                    raise Fault("CONTRACT_STALE", "Acceptance must bind the exact verified integration")
                if binding.get("decision", "approve") != "approve":
                    raise Fault("INVALID_REQUEST", "A final_accepted gate requires an approve decision")
                if not isinstance(binding.get("review_record_id"), str) or not binding["review_record_id"]:
                    raise Fault("INVALID_REQUEST", "Final approval requires an independent review record id")
                new_state = "ACCEPTED"
                invalidate = ()
            else:
                new_state = None
                invalidate = ("implementation_verified", "integrated_verified", "final_accepted")
            old = conn.execute("SELECT * FROM gates WHERE project_id=? AND task_id=? AND gate=?", (project_id, task_id, gate)).fetchone()
            changed = not old or old["artifact_digest"] != artifact_digest or _json(old["binding"]) != binding or _json(old["dependencies"]) != dependencies
            if not changed:
                return self._task_dict(conn, task)
            if changed:
                if gate in {"contract_ready", "implementation_verified"}:
                    self._revoke_history(conn, project_id, task_id)
                elif old:
                    self._append_gate_history(conn, old)
                for invalid in invalidate:
                    conn.execute("DELETE FROM gates WHERE project_id=? AND task_id=? AND gate=?", (project_id, task_id, invalid))
            seq = self._event(conn, "review_recorded" if gate == "final_accepted" else "gate_recorded", project_id, task_id,
                              gate=gate, artifact_digest=artifact_digest, binding=binding, principal_id=principal.id)
            conn.execute("INSERT OR REPLACE INTO gates VALUES(?,?,?,?,?,?,?,?)",
                         (project_id, task_id, gate, artifact_digest, canonical_json(binding), canonical_json(dependencies), principal.id, seq))
            self._append_gate_history(conn, conn.execute("SELECT * FROM gates WHERE project_id=? AND task_id=? AND gate=?",
                                                         (project_id, task_id, gate)).fetchone())
            if new_state:
                conn.execute("UPDATE tasks SET state=?,reason=NULL WHERE project_id=? AND task_id=?", (new_state, project_id, task_id))
            return self._task_dict(conn, self._task_row(conn, project_id, task_id))
        return self._mutate("gate.record", {"project_id": project_id, "task_id": task_id, "gate": gate,
                                           "artifact_digest": artifact_digest, "binding": binding},
                            principal, request_id, roles[gate], change)

    @staticmethod
    def _publication_dict(row: sqlite3.Row) -> dict:
        return {"project_id": row["project_id"], "publication_id": row["publication_id"],
                "plan": _json(row["plan"]), "principal_id": row["principal_id"],
                "created_seq": row["created_seq"], "active": True}

    def begin_publication(self, project_id: str, publication_id: str, plan_binding: dict,
                          principal: Principal, request_id: str) -> dict:
        """Acquire a durable publication lease without consuming a worker slot.

        The lease protects the plan/policy between source verification and the
        Git CAS plus gate recording. It remains held after a controller crash.
        Use a fresh request id on a later recovery invocation; replaying a prior
        request returns its historical result and does not renew an ended lease.
        """
        _identifier(publication_id, "publication_id")
        if not isinstance(plan_binding, dict):
            raise Fault("INVALID_REQUEST", "Publication must bind an exact plan")
        plan_binding = _json(canonical_json(plan_binding))
        def change(conn: sqlite3.Connection) -> dict:
            if plan_binding != _plan_binding(self._current_plan(conn, project_id)):
                raise Fault("CONTRACT_STALE", "Publication must bind the current approved plan exactly")
            existing = conn.execute("SELECT * FROM publications WHERE project_id=?", (project_id,)).fetchone()
            if existing:
                if existing["publication_id"] != publication_id or _json(existing["plan"]) != plan_binding:
                    raise Fault("SCOPE_CONFLICT", "Another publication already owns this project's integration boundary")
                if existing["principal_id"] != principal.id:
                    raise Fault("UNAUTHORIZED", "Publication lease belongs to another integrator identity")
                return self._publication_dict(existing)
            seq = self._event(conn, "publication_started", project_id,
                              publication_id=publication_id, plan=plan_binding, principal_id=principal.id)
            conn.execute("INSERT INTO publications VALUES(?,?,?,?,?)",
                         (project_id, publication_id, canonical_json(plan_binding), principal.id, seq))
            return self._publication_dict(conn.execute("SELECT * FROM publications WHERE project_id=?", (project_id,)).fetchone())
        return self._mutate("publication.begin", {"project_id": project_id, "publication_id": publication_id,
                                                 "plan": plan_binding}, principal, request_id, {"integrator"}, change)

    def end_publication(self, project_id: str, publication_id: str,
                        principal: Principal, request_id: str) -> dict:
        """Release an owned publication after receipt and gate reconciliation.

        A missing lease is an explicit no-op. A different publication or owner
        can never be released by an old completion request.
        """
        _identifier(publication_id, "publication_id")
        def change(conn: sqlite3.Connection) -> dict:
            self._project_row(conn, project_id)
            existing = conn.execute("SELECT * FROM publications WHERE project_id=?", (project_id,)).fetchone()
            if not existing:
                return {"project_id": project_id, "publication_id": publication_id, "released": False}
            if existing["publication_id"] != publication_id:
                raise Fault("SCOPE_CONFLICT", "Completion belongs to another publication")
            if existing["principal_id"] != principal.id:
                raise Fault("UNAUTHORIZED", "Only the owning integrator may release this publication")
            conn.execute("DELETE FROM publications WHERE project_id=?", (project_id,))
            self._event(conn, "publication_completed", project_id,
                        publication_id=publication_id, plan=_json(existing["plan"]), principal_id=principal.id)
            return {"project_id": project_id, "publication_id": publication_id, "released": True}
        return self._mutate("publication.end", {"project_id": project_id, "publication_id": publication_id},
                            principal, request_id, {"integrator"}, change)

    def block_task(self, project_id: str, task_id: str, reason: str,
                   principal: Principal, request_id: str) -> dict:
        """Record a concrete technical blocker; never release an active process slot.

        A blocked task is not retried automatically. The planner may register an
        explicitly scoped replacement task after resolving the reported blocker.
        """
        if not isinstance(reason, str) or not 1 <= len(reason) <= 8192 or "\x00" in reason:
            raise Fault("INVALID_REQUEST", "A bounded, nonempty blocker reason is required")
        def change(conn: sqlite3.Connection) -> dict:
            task = self._task_row(conn, project_id, task_id)
            self._assert_current(conn, task)
            if task["cancel_requested"]:
                raise Fault("CANCELLED", "Cancelled tasks cannot be reopened as blocked work")
            if conn.execute(
                    f"SELECT 1 FROM attempts WHERE project_id=? AND task_id=? AND state IN {ACTIVE_SQL}",
                    (project_id, task_id)).fetchone():
                raise Fault("INSTANCE_UNCERTAIN", "Confirm the active instance has exited before blocking delivery")
            self._revoke_history(conn, project_id, task_id)
            conn.execute("DELETE FROM gates WHERE project_id=? AND task_id=? AND gate!='contract_ready'",
                         (project_id, task_id))
            conn.execute("UPDATE tasks SET state='BLOCKED',reason=? WHERE project_id=? AND task_id=?",
                         (reason, project_id, task_id))
            self._event(conn, "task_blocked", project_id, task_id,
                        reason=reason, principal_id=principal.id)
            return self._task_dict(conn, self._task_row(conn, project_id, task_id))
        return self._mutate("task.block", {"project_id": project_id, "task_id": task_id, "reason": reason},
                            principal, request_id, {"coordinator", "verifier", "integrator"}, change)

    def review(self, project_id: str, task_id: str, artifact_digest: str, decision: str,
               binding: dict, principal: Principal, request_id: str) -> dict:
        """Record the configured independent reviewer's exact-artifact decision.

        Reject and rework preserve the candidate for inspection but do not enqueue
        another attempt. Rework requires a new explicitly registered task; it is
        never an implicit reset of the original task's retry or scope budget.
        """
        if decision not in {"approve", "reject", "rework"} or not isinstance(binding, dict):
            raise Fault("INVALID_REQUEST", "Review decision must be approve, reject, or rework")
        _artifact(artifact_digest)
        binding = _json(canonical_json(binding))
        if binding.get("decision", decision) != decision:
            raise Fault("INVALID_REQUEST", "Review binding contradicts the requested decision")
        binding["decision"] = decision
        if not isinstance(binding.get("review_record_id"), str) or not 1 <= len(binding["review_record_id"]) <= 200:
            raise Fault("INVALID_REQUEST", "An independent review record id is required")
        if decision == "approve":
            return self.record_gate(project_id, task_id, "final_accepted", artifact_digest,
                                    binding, principal, request_id)

        def change(conn: sqlite3.Connection) -> dict:
            task = self._task_row(conn, project_id, task_id)
            definition = self._assert_current(conn, task)
            project = self._project_row(conn, project_id)
            if principal.id != _json(project["config"])["review_authority"]:
                raise Fault("UNAUTHORIZED", "Only the configured independent reviewer may decide delivery")
            if task["cancel_requested"]:
                raise Fault("CANCELLED", "Cancelled tasks cannot receive a new review decision")
            if binding.get("plan") != definition["plan"]:
                raise Fault("CONTRACT_STALE", "Review must bind the exact current plan")
            integrated = self._valid_gate(conn, project_id, task_id, "integrated_verified")
            if not integrated or integrated["artifact_digest"] != artifact_digest:
                raise Fault("CONTRACT_STALE", "Review must bind the exact verified integration")
            if conn.execute(
                    f"SELECT 1 FROM attempts WHERE project_id=? AND task_id=? AND state IN {ACTIVE_SQL}",
                    (project_id, task_id)).fetchone():
                raise Fault("INSTANCE_UNCERTAIN", "Active work cannot receive a final review decision")
            reason = "REVIEW_REJECTED" if decision == "reject" else "REWORK_REQUIRED"
            self._revoke_history(conn, project_id, task_id)
            conn.execute("DELETE FROM gates WHERE project_id=? AND task_id=? AND gate='final_accepted'",
                         (project_id, task_id))
            conn.execute("UPDATE tasks SET state='BLOCKED',reason=? WHERE project_id=? AND task_id=?",
                         (reason, project_id, task_id))
            self._event(conn, "review_recorded", project_id, task_id,
                        decision=decision, artifact_digest=artifact_digest, binding=binding,
                        principal_id=principal.id, replacement_task_required=(decision == "rework"))
            return self._task_dict(conn, self._task_row(conn, project_id, task_id))
        return self._mutate("review.record", {"project_id": project_id, "task_id": task_id,
                                             "artifact_digest": artifact_digest, "decision": decision,
                                             "binding": binding}, principal, request_id, {"reviewer"}, change)

    def gate(self, project_id: str, task_id: str, gate: str, artifact_digest: str | None = None) -> dict:
        """Read a valid current proof or a non-revoked exact delivery history pin."""
        if gate not in GATES:
            raise Fault("INVALID_REQUEST", "Unknown dependency gate")
        if artifact_digest is not None:
            _artifact(artifact_digest)
        with closing(self._connect()) as conn:
            self._task_row(conn, project_id, task_id)
            row = self._valid_gate(conn, project_id, task_id, gate, artifact_digest=artifact_digest)
            if row is None:
                raise Fault("DEPENDENCY_BLOCKED", "Requested gate proof is unavailable, changed, or revoked")
            return {"artifact_digest": row["artifact_digest"], "binding": _json(row["binding"]),
                    "principal_id": row["principal_id"], "created_seq": row["created_seq"],
                    "historical": "epoch" in row.keys()}

    def project(self, project_id: str) -> dict:
        with closing(self._connect()) as conn:
            return self._project_dict(self._project_row(conn, project_id))

    def task(self, project_id: str, task_id: str) -> dict:
        with closing(self._connect()) as conn:
            return self._task_dict(conn, self._task_row(conn, project_id, task_id))

    def attempt(self, attempt_id: str) -> dict:
        with closing(self._connect()) as conn:
            return self._attempt_dict(conn, self._attempt_row(conn, attempt_id))

    def active_attempts(self, project_id: str | None = None) -> list[dict]:
        with closing(self._connect()) as conn:
            sql = f"SELECT * FROM attempts WHERE state IN {ACTIVE_SQL}"
            args = ()
            if project_id is not None:
                sql += " AND project_id=?"
                args = (project_id,)
            return [self._attempt_dict(conn, row) for row in conn.execute(sql + " ORDER BY created_seq", args)]

    def status(self, project_id: str | None = None) -> dict:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")  # All counters and task views come from one read snapshot.
            projects = conn.execute("SELECT * FROM projects" + (" WHERE id=?" if project_id else ""), (project_id,) if project_id else ()).fetchall()
            if project_id and not projects:
                raise Fault("NOT_FOUND", "Unknown project")
            limit = int(conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()[0])
            all_active = conn.execute(f"SELECT * FROM attempts WHERE state IN {ACTIVE_SQL}").fetchall()
            tasks = conn.execute("SELECT * FROM tasks" + (" WHERE project_id=?" if project_id else "") + " ORDER BY created_seq",
                                 (project_id,) if project_id else ()).fetchall()
            per_project = []
            for row in projects:
                result = self._project_dict(row)
                used = sum(attempt["project_id"] == row["id"] for attempt in all_active)
                project_limit = result["config"]["project_limit"]
                result["capacity"] = {"used": used, "limit": project_limit, "available": max(0, min(limit-len(all_active), project_limit-used))}
                per_project.append(result)
            active = [self._attempt_dict(conn, row) for row in all_active if project_id is None or row["project_id"] == project_id]
            cursor = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            publications = conn.execute("SELECT * FROM publications" + (" WHERE project_id=?" if project_id else "") + " ORDER BY created_seq",
                                        (project_id,) if project_id else ()).fetchall()
            return {"capacity": {"used": len(all_active), "limit": limit, "available": max(0, limit-len(all_active)),
                                 "uncertain": sum(row["state"] == "LOST" for row in all_active)},
                    "projects": per_project, "tasks": [self._task_dict(conn, row) for row in tasks],
                    "active_attempts": active, "pending_publications": [self._publication_dict(row) for row in publications],
                    "event_cursor": cursor}

    def events(self, after: int = 0, limit: int = 1000) -> list[dict]:
        _integer(after, "event cursor", minimum=0, maximum=9_223_372_036_854_775_807)
        _integer(limit, "event limit", maximum=1000)
        with closing(self._connect()) as conn:
            return [{**dict(row), "payload": _json(row["payload"])} for row in conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]
