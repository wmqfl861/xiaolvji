"""Local capability authentication; no role supplied in a request is trusted.

Files are a same-user cooperative boundary only. Strict execution must deny
worker access to this entire directory using a verified OS boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import tempfile
from pathlib import Path

from .contracts import Fault, Principal, canonical_json, read_json

ROLES = ("operator", "planner", "coordinator", "executor", "verifier", "integrator", "reviewer", "reader")


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(canonical_json(value) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Authority:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir).absolute()
        if self.root.resolve() != self.root:
            raise Fault("UNSAFE_STATE", "State directory cannot contain symlink aliases.")
        self.path = self.root / "authority.json"

    def initialize(self) -> dict:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock = self.root / "authority.init.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise Fault("INSTANCE_UNCERTAIN", "Authority initialization is already in progress.") from exc
        try:
            os.close(fd)
            if self.path.exists():
                self._records()
                return {"initialized": True, "created": False, "identities": self._paths()}
            keys_dir = self.root / "identities"
            try:
                keys_dir.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError as exc:
                raise Fault("INSTANCE_UNCERTAIN", "Partial authority initialization requires operator inspection.") from exc
            records = []
            for role in ROLES:
                token = secrets.token_urlsafe(48)
                key_path = keys_dir / (role + ".key")
                kfd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(kfd, "w", encoding="ascii") as f:
                    f.write(token + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                records.append({"id": role, "role": role, "digest": hashlib.sha256(token.encode()).hexdigest()})
            atomic_json(self.path, {"version": 1, "principals": records})
            return {"initialized": True, "created": True, "identities": self._paths()}
        finally:
            lock.unlink(missing_ok=True)

    def _paths(self):
        return {role: str(self.root / "identities" / (role + ".key")) for role in ROLES}

    def _records(self):
        if self.path.is_symlink():
            raise Fault("UNSAFE_STATE", "Authority file cannot be a symlink.")
        data = read_json(self.path)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("principals"), list):
            raise Fault("UNSAFE_STATE", "Unsupported authority store.")
        seen = set()
        hashes = set()
        for record in data["principals"]:
            if (not isinstance(record, dict) or set(record) != {"id", "role", "digest"} or
                    not isinstance(record["id"], str) or not record["id"] or
                    record["role"] not in ROLES or not isinstance(record["digest"], str) or
                    not re.fullmatch(r"[a-f0-9]{64}", record["digest"]) or
                    record["id"] in seen or record["digest"] in hashes):
                raise Fault("UNSAFE_STATE", "Invalid or duplicated authority record.")
            seen.add(record["id"])
            hashes.add(record["digest"])
        return data["principals"]

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise Fault("UNAUTHORIZED", "Invalid capability.")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        for record in self._records():
            if hmac.compare_digest(record["digest"], hashed):
                return Principal(record["id"], record["role"])
        raise Fault("UNAUTHORIZED", "Unknown capability.")

    def from_file(self, path: str | Path | None = None) -> Principal:
        path = Path(path) if path else self.root / "identities" / "operator.key"
        if path.is_symlink() or not path.is_file():
            raise Fault("UNAUTHORIZED", "Identity file must be an existing regular file.")
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise Fault("UNSAFE_STATE", "Identity file must not be readable by group or others.")
        try:
            with path.open(encoding="ascii") as f:
                token = f.read(257).strip()
        except (OSError, UnicodeError) as exc:
            raise Fault("UNAUTHORIZED", "Cannot read identity file.") from exc
        return self.authenticate(token)


def require(principal: Principal, *roles: str):
    if not isinstance(principal, Principal) or principal.role not in roles:
        raise Fault("UNAUTHORIZED", "This operation requires a different authority.")
