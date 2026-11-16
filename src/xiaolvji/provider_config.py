"""Explicit provider and host snapshots; no ambient account/config discovery."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from .contracts import Fault, canonical_json, digest, load_json, read_json, validate_identifier
from .host_config import derive_request_constraints, normalize_host_snapshot
from .provider_broker import normalize_provider_profile


@dataclass(frozen=True)
class ProviderConfiguration:
    _json: str = field(repr=False)

    @property
    def value(self):
        return load_json(self._json, limit=1024 * 1024)

    @property
    def digest(self):
        return digest(self.value)

    def host(self, project_id, engine):
        for binding in self.value["bindings"]:
            if binding["project_id"] == project_id and binding["host"]["engine"] == engine:
                return binding["host"]
        raise Fault("HOST_NOT_CONFIGURED", "No explicit host snapshot matches this project and engine.")

    def profiles(self):
        return {item["profile_id"]: normalize_provider_profile(item) for item in self.value["providers"]}

    def summary(self):
        value = self.value
        return {"schema_version": 1, "configuration_digest": self.digest,
                "providers": [{"profile_id": item["profile_id"], "api": item["api"], "model": item["model"],
                               "policy_digest": digest(item)} for item in value["providers"]],
                "bindings": [{"project_id": item["project_id"], "engine": item["host"]["engine"],
                              "snapshot_digest": digest(item["host"])} for item in value["bindings"]],
                "global_request_limit": value["global_request_limit"],
                "credentials_read": False, "native_host_execution_verified": False}


def normalize_provider_configuration(value):
    value = load_json(canonical_json(value), limit=1024 * 1024)
    fields = {"schema_version", "providers", "bindings", "credential_files", "global_request_limit"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Fault("INVALID_PROVIDER_CONFIG", "Provider configuration must match the closed version-one schema.")
    if type(value["global_request_limit"]) is not int or not 1 <= value["global_request_limit"] <= 64:
        raise Fault("INVALID_PROVIDER_CONFIG", "Global local-request limit must be between one and 64.")
    if not isinstance(value["providers"], list) or not 1 <= len(value["providers"]) <= 16:
        raise Fault("INVALID_PROVIDER_CONFIG", "Configuration requires one to 16 explicit provider profiles.")
    profiles = {}
    for item in value["providers"]:
        profile = normalize_provider_profile(item)
        if profile.profile_id in profiles:
            raise Fault("INVALID_PROVIDER_CONFIG", "Provider profile identifiers must be unique.")
        profiles[profile.profile_id] = profile
    if not isinstance(value["credential_files"], dict) or set(value["credential_files"]) != set(profiles):
        raise Fault("INVALID_PROVIDER_CONFIG", "Each provider requires one explicit credential-file reference.")
    for path in value["credential_files"].values():
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096 or any(part in {"", ".", ".."} for part in path.split("/")[1:]):
            raise Fault("INVALID_PROVIDER_CONFIG", "Credential references must be canonical absolute file paths.")
    if not isinstance(value["bindings"], list) or not 1 <= len(value["bindings"]) <= 256:
        raise Fault("INVALID_PROVIDER_CONFIG", "Configuration requires bounded project/host bindings.")
    bindings, seen = [], set()
    for item in value["bindings"]:
        if not isinstance(item, dict) or set(item) != {"project_id", "host"}:
            raise Fault("INVALID_PROVIDER_CONFIG", "Host bindings require project_id and host only.")
        validate_identifier(item["project_id"], "provider project binding")
        host = normalize_host_snapshot(item["host"])
        key = (item["project_id"], host["engine"])
        if key in seen or host["provider_id"] not in profiles:
            raise Fault("INVALID_PROVIDER_CONFIG", "Host bindings must be unique and use registered providers.")
        seen.add(key)
        profile = profiles[host["provider_id"]]
        constraints = derive_request_constraints(host)
        model = constraints.pop("model")
        if profile.api != host["api"] or profile.model != model or canonical_json(profile.to_dict()["request_constraints"]) != canonical_json(constraints):
            raise Fault("PROVIDER_QUALITY_MISMATCH", "Provider model, API and request constraints must exactly match the explicit host snapshot.")
        bindings.append({"project_id": item["project_id"], "host": host})
    normalized = {**value, "providers": [p.to_dict() for p in profiles.values()], "bindings": bindings}
    return ProviderConfiguration(canonical_json(normalized))


def load_provider_configuration(path):
    # This function does not inspect, open, or authenticate any credential.
    return normalize_provider_configuration(read_json(path))


def read_explicit_credentials(configuration: ProviderConfiguration, *, forbidden_roots=()):
    """Read only named controller-owned private files at actual broker startup.

    Files under enrolled sources/workspaces are rejected. Existing Codex/ZCode
    account stores, ambient provider environment variables and OAuth are never
    searched or copied. Errors contain no paths, bytes or raw OS exceptions.
    """
    from .sandbox import readable_host_sources
    # Same source inventory as actual sandbox mounts, including interpreter
    # prefixes and resolved aliases. Read-only files are still readable by the
    # mapped worker UID; mode0600 alone does not protect such credential paths.
    forbidden_locations = tuple(Path(root).resolve() for root in forbidden_roots) + readable_host_sources()
    result = {}
    for name, configured in configuration.value["credential_files"].items():
        path = Path(configured)
        try:
            if path.resolve(strict=True) != path or any(path.is_relative_to(root) for root in forbidden_locations):
                raise ValueError("unsafe credential location")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 8193:
                    raise ValueError("private regular file required")
                raw = os.read(descriptor, 8194)
            finally:
                os.close(descriptor)
            token = raw.decode("ascii")
            if token.endswith("\n"):
                token = token[:-1]
            if not 8 <= len(token) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("invalid token")
            result[name] = token
        except (OSError, ValueError, UnicodeError, RuntimeError):
            result.clear()
            raise Fault("PROVIDER_CREDENTIAL_UNAVAILABLE", "An explicitly configured provider credential is missing or not a private, safe token file.") from None
    return result
