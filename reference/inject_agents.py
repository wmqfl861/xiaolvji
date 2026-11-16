#!/usr/bin/env python3
"""Inspect, apply, or remove one project-local English AGENTS policy block.

This is a reference component, not an agent runtime or a security sandbox.
It never searches for projects, changes global configuration, or trusts Hooks.
All writes require the explicit ``apply`` or ``remove`` action and --project.
Metadata and original-byte backups remain in the selected project's
``.parallel-dev`` directory after removal. An interrupted multi-file operation
leaves a pending journal and requires inspection instead of automatic guessing.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid


BEGIN_TOKEN = b"<!-- parallel-development:managed-policy:begin"
END_TOKEN = b"<!-- parallel-development:managed-policy:end"
END_MARKER = END_TOKEN + b" -->"
BOM = b"\xef\xbb\xbf"
TARGETS = ("AGENTS.md", "AGENTS.override.md")
STATE_NAME = "agents-injection.json"
PENDING_NAME = "agents-injection.pending.json"
LOCK_NAME = "agents-injection.lock"
MAX_BYTES = 8 * 1024 * 1024
MAX_POLICY_BYTES = 128 * 1024
COMPONENT = "parallel-development-agents-injector"
DEFAULT_POLICY = Path(__file__).parent.parent / "spec" / "AGENTS.block.en.md"


class InjectionError(Exception):
    """A validation/update error that must not be treated as successful installation."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decoded(value: object) -> bytes:
    if not isinstance(value, str):
        raise InjectionError("Invalid byte encoding in the injection manifest.")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise InjectionError("Invalid byte encoding in the injection manifest.") from exc


def check_directory(path: Path) -> None:
    """Check each existing component without resolving away a symlink."""
    for component in (path, *path.parents):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError as exc:
            raise InjectionError(f"Directory does not exist: {component}") from exc
        if stat.S_ISLNK(mode):
            raise InjectionError(f"Symbolic-link directories are not supported: {component}")
        if not stat.S_ISDIR(mode):
            raise InjectionError(f"Expected a regular directory: {component}")


def read_file(path: Path) -> tuple[bytes | None, int | None]:
    """Read a bounded regular file; reject symlinks and special files first."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InjectionError(f"Expected a regular file, not a symlink or special file: {path}")
    if info.st_size > MAX_BYTES:
        raise InjectionError(f"File exceeds the {MAX_BYTES}-byte reference-tool limit: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InjectionError(f"Cannot safely open file: {path}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise InjectionError(f"File changed to a non-regular object: {path}")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InjectionError(f"File grew beyond the reference-tool limit: {path}")
    return data, stat.S_IMODE(opened.st_mode)


def case_guard(directory: Path, canonical_names: tuple[str, ...]) -> None:
    """Avoid creating competing guidance names on case-insensitive systems."""
    if not directory.exists():
        return
    check_directory(directory)
    names = [entry.name for entry in directory.iterdir()]
    for canonical in canonical_names:
        matches = [name for name in names if name.casefold() == canonical.casefold()]
        if len(matches) > 1 or (matches and matches[0] != canonical):
            raise InjectionError(
                f"Case-variant or duplicate name for {canonical}: {', '.join(matches)}. "
                "Resolve the naming conflict explicitly before using this tool."
            )


def block_span(data: bytes) -> tuple[int, int] | None:
    begin_count, end_count = data.count(BEGIN_TOKEN), data.count(END_TOKEN)
    if begin_count == end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise InjectionError("Missing, duplicate, or malformed managed policy markers.")
    start, closing = data.index(BEGIN_TOKEN), data.index(END_TOKEN)
    if closing <= start:
        raise InjectionError("Managed policy markers are out of order.")
    for position in (start, closing):
        if position != 0 and data[position - 1:position] != b"\n":
            if not (position == len(BOM) and data.startswith(BOM)):
                raise InjectionError("Managed policy markers must start on their own lines.")
    first_newline = data.find(b"\n", start)
    if first_newline < 0:
        raise InjectionError("The managed begin marker is incomplete.")
    first_line = data[start:first_newline + 1]
    if not re.fullmatch(
        rb'<!-- parallel-development:managed-policy:begin version="[A-Za-z0-9._+-]+" -->\r?\n',
        first_line,
    ):
        raise InjectionError("The managed begin marker has an unsupported format.")
    end_newline = data.find(b"\n", closing)
    end = len(data) if end_newline < 0 else end_newline + 1
    last_line = data[closing:end]
    if last_line not in (END_MARKER, END_MARKER + b"\n", END_MARKER + b"\r\n"):
        raise InjectionError("The managed end marker must occupy its complete line.")
    return start, end


def load_policy(path: Path) -> bytes:
    check_directory(path.parent.absolute())
    raw, _ = read_file(path)
    if raw is None:
        raise InjectionError(f"English policy source is missing: {path}")
    if len(raw) > MAX_POLICY_BYTES:
        raise InjectionError("The policy source exceeds the 128 KiB reference-tool limit.")
    if raw.startswith(BOM):
        raw = raw[len(BOM):]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InjectionError("The policy source must be UTF-8 text.") from exc
    if re.search(r"[\u3400-\u9fff]", text):
        raise InjectionError("The injected policy source must not contain CJK instructions.")
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized:
        raise InjectionError("The policy source contains unsupported bare carriage returns.")
    result = normalized.encode("utf-8")
    if not result.endswith(b"\n"):
        result += b"\n"
    if block_span(result) != (0, len(result)):
        raise InjectionError("The policy source must contain exactly one complete managed block.")
    return result


def newline_for(data: bytes) -> bytes:
    first = data.find(b"\n")
    return b"\r\n" if first > 0 and data[first - 1:first] == b"\r" else b"\n"


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def atomic_write(path: Path, value: bytes, expected: bytes | None, mode: int = 0o600) -> None:
    check_directory(path.parent)
    observed, _ = read_file(path)
    if observed != expected:
        raise InjectionError(f"File changed since inspection; refusing to overwrite it: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        check_directory(path.parent)
        observed, _ = read_file(path)
        if observed != expected:
            raise InjectionError(f"File changed while preparing an update: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def guarded_delete(path: Path, expected: bytes) -> None:
    check_directory(path.parent)
    observed, _ = read_file(path)
    if observed != expected:
        raise InjectionError(f"File changed; refusing to delete it: {path}")
    path.unlink()


class Injector:
    def __init__(self, project: str | Path, target: str | None = None, policy_path: Path | None = None):
        supplied = Path(project)
        if not supplied.is_absolute() or ".." in supplied.parts:
            raise InjectionError("--project must be an explicit absolute path without '..' components.")
        if supplied == Path(supplied.anchor):
            raise InjectionError("The filesystem root cannot be used as a project.")
        check_directory(supplied)
        if target is not None and target not in TARGETS:
            raise InjectionError("--target must be AGENTS.md or the existing AGENTS.override.md.")
        self.project = supplied
        self.target_name = target or "AGENTS.md"
        self.target = supplied / self.target_name
        self.metadata = supplied / ".parallel-dev"
        self.backups = self.metadata / "backups"
        self.manifest_path = self.metadata / STATE_NAME
        self.pending_path = self.metadata / PENDING_NAME
        self.lock_path = self.metadata / LOCK_NAME
        self.policy = load_policy((policy_path or DEFAULT_POLICY).absolute())

    def validate_paths(self) -> None:
        check_directory(self.project)
        case_guard(self.project, TARGETS + (".parallel-dev",))
        for name in TARGETS:
            read_file(self.project / name)
        if self.metadata.exists() or self.metadata.is_symlink():
            check_directory(self.metadata)
            case_guard(self.metadata, (STATE_NAME, PENDING_NAME, LOCK_NAME, "backups"))
            for name in (STATE_NAME, PENDING_NAME, LOCK_NAME):
                read_file(self.metadata / name)
            if self.backups.exists() or self.backups.is_symlink():
                check_directory(self.backups)

    def load_manifest(self) -> tuple[dict | None, bytes | None, bytes | None]:
        raw, _ = read_file(self.manifest_path)
        if raw is None:
            return None, None, None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InjectionError("The injection manifest is invalid; preserve it for inspection.") from exc
        if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("component") != COMPONENT:
            raise InjectionError("Unrecognized injection manifest schema or owner.")
        if record.get("project") != str(self.project) or record.get("target") not in TARGETS:
            raise InjectionError("The injection manifest belongs to a different project or target.")
        if record.get("state") not in ("active", "removed") or type(record.get("original_existed")) is not bool:
            raise InjectionError("Invalid installation state in the injection manifest.")
        backup_name = record.get("backup_file")
        if not isinstance(backup_name, str) or not re.fullmatch(r"[a-f0-9]{64}-[a-f0-9]{12}\.bin", backup_name):
            raise InjectionError("Invalid backup reference in the injection manifest.")
        check_directory(self.backups)
        original, _ = read_file(self.backups / backup_name)
        if original is None or digest(original) != record.get("original_sha256"):
            raise InjectionError("The original-byte backup is missing or has changed.")
        managed = decoded(record.get("managed_base64"))
        if digest(managed) != record.get("managed_sha256") or block_span(managed) != (0, len(managed)):
            raise InjectionError("The recorded managed block is invalid.")
        if decoded(record.get("separator_base64")) not in (b"", b"\n", b"\r\n"):
            raise InjectionError("The recorded insertion separator is invalid.")
        return record, raw, original

    def snapshot(self) -> dict:
        self.validate_paths()
        record, manifest_raw, original = self.load_manifest()
        data, mode = read_file(self.target)
        span = block_span(data or b"")
        override, _ = read_file(self.project / "AGENTS.override.md")
        pending, _ = read_file(self.pending_path)
        return dict(record=record, manifest_raw=manifest_raw, original=original,
                    data=data, mode=mode, span=span, override=override, pending=pending)

    def inspect(self) -> dict:
        view = self.snapshot()
        warnings = []
        if view["override"] is not None:
            warnings.append(
                "AGENTS.override.md exists. In Codex, a non-empty override takes precedence "
                "over AGENTS.md at this scope; inspect the host's effective instruction chain."
            )
        if view["pending"] is not None:
            status = "interrupted_operation"
            warnings.append("A pending journal exists. Reconcile it before another mutation.")
        elif view["record"] and view["record"]["state"] == "active":
            record = view["record"]
            if record["target"] != self.target_name:
                status = "installed_in_other_target"
                warnings.append(f"Use --target {record['target']} to inspect or remove the installed block.")
            elif view["span"] is None:
                status = "managed_block_missing"
            elif digest(view["data"][slice(*view["span"])]) != record["managed_sha256"]:
                status = "managed_block_modified"
            else:
                status = "installed"
        elif view["span"] is not None:
            status = "unmanaged_block"
        else:
            status = "not_installed"
        return {"action": "inspect", "status": status, "project": str(self.project),
                "target": self.target_name, "target_exists": view["data"] is not None,
                "writes_performed": False, "warnings": warnings}

    def plan(self, action: str) -> dict:
        view = self.snapshot()
        if view["pending"] is not None:
            raise InjectionError("A pending operation journal exists. Inspect and reconcile it before writing.")
        if action == "apply":
            if view["override"] is not None and self.target_name != "AGENTS.override.md":
                raise InjectionError(
                    "AGENTS.override.md exists and can take precedence over AGENTS.md in Codex. "
                    "Inspect effective guidance; explicitly select --target AGENTS.override.md "
                    "only if you intend to update that existing file."
                )
            if self.target_name == "AGENTS.override.md" and view["data"] is None:
                raise InjectionError("This tool never creates a new AGENTS.override.md file.")
        record, data, span = view["record"], view["data"], view["span"]
        active = record is not None and record["state"] == "active"
        if active and record["target"] != self.target_name:
            raise InjectionError(f"An active installation targets {record['target']}; select it explicitly first.")
        if active:
            if data is None or span is None:
                raise InjectionError("The installed target or managed block is missing; refusing to recreate or guess.")
            if digest(data[slice(*span)]) != record["managed_sha256"]:
                raise InjectionError("The managed block was edited. Preserve those edits and resolve the conflict manually.")
        elif span is not None:
            raise InjectionError("A managed-looking block has no active ownership record; refusing to adopt or remove it.")

        if action == "remove":
            if not active:
                return {"noop": True, "status": "not_installed"}
            start, end = span
            separator = decoded(record["separator_base64"])
            # Restore an unchanged, newline-less original exactly. If outside
            # content changed, retain outside bytes instead of guessing intent.
            if separator and data[:start] == view["original"] + separator and not data[end:]:
                start -= len(separator)
            after = data[:start] + data[end:]
            if not record["original_existed"] and after == b"":
                after = None
            updated = dict(record, state="removed")
            return dict(view, after=after, updated=updated, backup=None, status="removed")

        line_ending = newline_for(data or b"")
        managed = self.policy.replace(b"\n", line_ending)
        if active:
            if data[slice(*span)] == managed:
                return {"noop": True, "status": "unchanged"}
            after = data[:span[0]] + managed + data[span[1]:]
            updated = dict(record, managed_base64=encoded(managed), managed_sha256=digest(managed))
            return dict(view, after=after, updated=updated, backup=None, status="updated")

        original = data or b""
        separator = b"" if not original or original == BOM or original.endswith(b"\n") else line_ending
        backup_name = f"{digest(original)}-{uuid.uuid4().hex[:12]}.bin"
        updated = {
            "schema_version": 1, "component": COMPONENT, "project": str(self.project),
            "target": self.target_name, "state": "active", "original_existed": data is not None,
            "original_sha256": digest(original), "backup_file": backup_name,
            "managed_base64": encoded(managed), "managed_sha256": digest(managed),
            "separator_base64": encoded(separator),
        }
        return dict(view, after=original + separator + managed, updated=updated,
                    backup=(backup_name, original), status="applied")

    @contextlib.contextmanager
    def mutation_lock(self):
        self.validate_paths()
        self.metadata.mkdir(mode=0o700, exist_ok=True)
        check_directory(self.metadata)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise InjectionError("Another injection operation may be active; inspect the existing lock before retrying.") from exc
        lock_bytes = f"pid={os.getpid()}\n".encode("ascii")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(lock_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            current, _ = read_file(self.lock_path)
            if current == lock_bytes:
                self.lock_path.unlink()

    def execute(self, action: str = "inspect") -> dict:
        if action == "inspect":
            return self.inspect()
        if action not in ("apply", "remove"):
            raise InjectionError("Action must be inspect, apply, or remove.")
        first = self.plan(action)
        if first.get("noop"):
            return {"action": action, "status": first["status"], "target": self.target_name,
                    "project": str(self.project), "writes_performed": False}
        if first["after"] is not None and len(first["after"]) > MAX_BYTES:
            raise InjectionError("The resulting instruction file would exceed the reference-tool size limit.")
        with self.mutation_lock():
            change = self.plan(action)
            if change.get("noop"):
                return {"action": action, "status": change["status"], "target": self.target_name,
                        "project": str(self.project), "writes_performed": False}
            if change["after"] is not None and len(change["after"]) > MAX_BYTES:
                raise InjectionError("The resulting instruction file would exceed the reference-tool size limit.")
            self.backups.mkdir(mode=0o700, exist_ok=True)
            check_directory(self.backups)
            if change["backup"]:
                name, original = change["backup"]
                atomic_write(self.backups / name, original, None)
            journal = json_bytes({
                "schema_version": 1, "component": COMPONENT, "operation": action,
                "target": self.target_name,
                "before_sha256": None if change["data"] is None else digest(change["data"]),
                "after_sha256": None if change["after"] is None else digest(change["after"]),
                "next_manifest": change["updated"],
            })
            atomic_write(self.pending_path, journal, None)
            if change["after"] is None:
                guarded_delete(self.target, change["data"])
            else:
                mode = change["mode"] if change["mode"] is not None else 0o644
                atomic_write(self.target, change["after"], change["data"], mode)
            atomic_write(self.manifest_path, json_bytes(change["updated"]), change["manifest_raw"])
            guarded_delete(self.pending_path, journal)
        return {"action": action, "status": change["status"], "project": str(self.project),
                "target": self.target_name, "writes_performed": True,
                "metadata": str(self.metadata), "backups_retained": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("inspect", "apply", "remove"), default="inspect")
    parser.add_argument("--project", required=True, help="Explicit absolute path to an existing project directory")
    parser.add_argument("--target", choices=TARGETS, help="Canonical target; an override must already exist")
    arguments = parser.parse_args(argv)
    try:
        result = Injector(arguments.project, arguments.target).execute(arguments.action)
    except (InjectionError, OSError) as exc:
        print(json.dumps({"status": "not_completed", "error": str(exc),
                          "inspect_before_retry": True}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
