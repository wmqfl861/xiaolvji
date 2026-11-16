"""Shared, deterministic validation. No host facts or authorization inferred here."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

MAX_JSON_BYTES = 2 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class Fault(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self):
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class Principal:
    """Construct only after transport authentication; never deserialize caller roles."""
    id: str
    role: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")  # Reject unpaired surrogate code points.
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Fault("INVALID_INPUT", "Value is not finite JSON.") from exc


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_json(text: str | bytes, limit: int = MAX_JSON_BYTES) -> Any:
    try:
        size = len(text.encode("utf-8") if isinstance(text, str) else text)
    except UnicodeError as exc:
        raise Fault("INVALID_INPUT", "JSON must contain valid Unicode scalar values.") from exc
    if size > limit:
        raise Fault("INPUT_TOO_LARGE", "JSON input exceeds the size limit.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Non-finite JSON number")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        # Bound nesting even for inputs accepted by the platform parser.
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 48:
                raise ValueError("JSON nesting exceeds 48 levels")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                stack.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item)
            elif isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Non-finite JSON number")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Fault("INVALID_INPUT", str(exc)) from exc


def read_json(path: str | Path) -> Any:
    try:
        with open(path, "rb") as f:
            return load_json(f.read(MAX_JSON_BYTES + 1))
    except OSError as exc:
        raise Fault("IO_ERROR", "Cannot read JSON file.", {"path": str(path), "error": str(exc)}) from exc


def validate_identifier(value: Any, label: str = "identifier") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise Fault("INVALID_INPUT", f"Invalid {label}.")
    return value


def validate_digest(value: Any) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise Fault("INVALID_INPUT", "Expected a sha256 digest.")
    return value


# Deliberately a validator for our closed task-schema keyword set, not a claim
# of general Draft 2020-12 conformance. Unknown keywords fail rather than vanish.
_SCHEMA_KEYS = {"$schema", "$id", "title", "description", "default", "type", "const", "enum",
                "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
                "uniqueItems", "minLength", "maxLength", "pattern", "minimum", "maximum"}


def _validate(schema: dict, value: Any, path: str = "task") -> None:
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise Fault("SCHEMA_UNSUPPORTED", "Unsupported validation keywords.", sorted(unknown))
    def reject(message):
        raise Fault("INVALID_TASK", path + ": " + message)
    kind = schema.get("type")
    types = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
             "string": lambda x: isinstance(x, str), "integer": lambda x: type(x) is int,
             "boolean": lambda x: type(x) is bool}
    if kind is not None and (kind not in types or not types[kind](value)):
        reject("expected " + str(kind))
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        reject("incorrect constant")
    if "enum" in schema and not any(type(value) is type(x) and value == x for x in schema["enum"]):
        reject("unsupported value")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            reject("missing " + ", ".join(sorted(missing)))
        extra = set(value) - set(props)
        if schema.get("additionalProperties") is False and extra:
            reject("unknown fields " + ", ".join(sorted(extra)))
        for key in value:
            if key in props:
                _validate(props[key], value[key], path + "." + key)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 4096):
            reject("invalid list length")
        if schema.get("uniqueItems") and len(set(canonical_json(x) for x in value)) != len(value):
            reject("duplicate list entries")
        for i, item in enumerate(value):
            if "items" in schema:
                _validate(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 65536):
            reject("invalid string length")
        if "\x00" in value:
            reject("NUL is not permitted")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            reject("invalid string format")
    if type(value) is int:
        if not schema.get("minimum", -(2**53)) <= value <= schema.get("maximum", 2**53):
            reject("out-of-range integer")


def validate_task(task: Any) -> dict:
    # Round-trip validates JSON only, isolates caller mutations, and limits size.
    task = load_json(canonical_json(task))
    schema = read_json(Path(__file__).parent / "resources" / "task.schema.json")
    _validate(schema, task)
    for field in ("contract_versions", "acceptance"):
        ids = [entry["id"] for entry in task[field]]
        if len(set(ids)) != len(ids):
            raise Fault("INVALID_TASK", f"Duplicate ids in {field}.")
    dependencies = [(d["task_id"], d["gate"]) for d in task["depends_on"]]
    if len(set(dependencies)) != len(dependencies) or any(d[0] == task["task_id"] for d in dependencies):
        raise Fault("INVALID_TASK", "Repeated or self dependency.")
    for entry in task["write_scope"]:
        _relative(entry["path"])
    for check in task["acceptance"]:
        cwd = check.get("cwd", ".")
        if cwd != ".":
            _relative(cwd)
        if "argv" in check and not check["argv"][0]:
            raise Fault("INVALID_TASK", "Empty executable in acceptance command.")
        if check.get("timeout_ms", 1) > 86_400_000:
            raise Fault("INVALID_TASK", "Acceptance timeout exceeds one day.")
    if task["retry"]["backoff_ms"] > 86_400_000:
        raise Fault("INVALID_TASK", "Retry backoff exceeds one day.")
    return task


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise Fault("INVALID_SCOPE", "Expected a bounded relative path.")
    if any(ord(c) < 32 for c in value) or any(c in value for c in "\\:*?[]{}"):
        raise Fault("INVALID_SCOPE", "Path contains unsupported platform or glob characters.")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise Fault("INVALID_SCOPE", "Paths must be normalized and repository-relative.")
    reserved = {".git", ".parallel-dev"}
    if any(part.casefold() in reserved or part.endswith((".", " ")) for part in p.parts):
        raise Fault("INVALID_SCOPE", "Path addresses protected metadata or a platform alias.")
    # Conservative Windows device aliases are rejected on every platform.
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in p.parts):
        raise Fault("INVALID_SCOPE", "Reserved device path.")
    return p.as_posix()


def normalize_scope(root: str | Path, scopes: list[dict]) -> list[dict]:
    root = Path(root).absolute()
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise Fault("INVALID_SCOPE", "Repository root must be a real canonical directory.")
    result = []
    for scope in scopes:
        if not isinstance(scope, dict) or scope.get("kind") not in ("file", "directory"):
            raise Fault("INVALID_SCOPE", "Invalid scope entry.")
        relative = _relative(scope.get("path"))
        target = root
        for part in PurePosixPath(relative).parts:
            if target.is_dir():
                matches = [p.name for p in target.iterdir() if p.name.casefold() == part.casefold()]
                if any(m != part for m in matches):
                    raise Fault("INVALID_SCOPE", "Path case alias or collision.")
            if target.exists() and not target.is_dir():
                raise Fault("INVALID_SCOPE", "A regular file cannot be a path ancestor.")
            target = target / part
            if target.is_symlink():
                raise Fault("INVALID_SCOPE", "Symlink components cannot own writable scope.")
        canonical = str(target.resolve(strict=False))
        if not target.resolve(strict=False).is_relative_to(root):
            raise Fault("INVALID_SCOPE", "Scope escapes repository root.")
        if target.exists() and ((scope["kind"] == "file" and not target.is_file()) or
                                (scope["kind"] == "directory" and not target.is_dir())):
            raise Fault("INVALID_SCOPE", "Scope kind disagrees with filesystem.")
        result.append({"path": relative, "kind": scope["kind"], "canonical": canonical,
                       "key": os.path.normcase(canonical).replace("\\", "/").casefold()})
    return result


def _key(scope: dict) -> str:
    return str(scope.get("key", scope.get("canonical", scope["path"]))).replace("\\", "/").rstrip("/").casefold()


def scopes_overlap(left: list[dict], right: list[dict]) -> bool:
    for a in left:
        for b in right:
            ka, kb = _key(a), _key(b)
            if ka == kb or (a["kind"] == "directory" and kb.startswith(ka + "/")) or (
                    b["kind"] == "directory" and ka.startswith(kb + "/")):
                return True
    return False


def path_in_scope(path: str, scopes: list[dict]) -> bool:
    relative = _relative(path)
    return any(relative == s["path"] or
               (s["kind"] == "directory" and relative.startswith(s["path"].rstrip("/") + "/"))
               for s in scopes)
