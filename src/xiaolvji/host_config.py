"""Explicit host configuration, without ambient config or credential discovery.

Rendering is not a host-readiness probe. Returned file bodies / environment may
contain an ephemeral broker capability and must not be written to audit logs.
"""
from __future__ import annotations

import re
from typing import Any

from .contracts import Fault, canonical_json, digest, load_json, validate_identifier


PRIVATE_HOME = "/home/xiaolvji"
BRIDGE_PROVIDER_ID = "xiaolvji_bridge"
CAPABILITY_ENV = "XIAOLVJI_PROVIDER_CAPABILITY"
CODEX_CONFIG_PATH = ".codex/config.toml"
CODEX_CATALOG_PATH = ".codex/model-catalog.json"
MAX_CODEX_CATALOG_BYTES = 1024 * 1024
ZCODE_PERSONAL_PATH = ".zcode/v2/provider_config.json"
ZCODE_BUILTIN_PATH = ".zcode/v2/xiaolvji_builtin_config.json"
ZCODE_SOURCE_COMMIT = "a60ba6a7e5a06742270ca605f5d2cc2766afa7e1"

_APIS = {"openai-responses", "openai-chat-completions", "anthropic-messages"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,254}\Z", re.ASCII)
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,256}\Z", re.ASCII)
_BRIDGE_ORIGIN = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z", re.ASCII)
_CODEX_QUALITY = {
    "model_reasoning_effort", "model_reasoning_summary", "model_verbosity",
    "service_tier", "model_context_window", "model_auto_compact_token_limit",
}
_ZCODE_PROPERTIES = {
    "requiresMfjsToolSchema", "contextWindow", "inputFormat", "outputFormat",
    "supportsToolCall", "supportsJsonSchemaOutput", "supportsNativeWebSearch",
    "supportsMidConversationSystem",
}
_ZCODE_INPUT = {"supportsText", "supportsImage", "supportsVideo", "supportsAudio", "supportsPdf"}


def _object(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise Fault("INVALID_INPUT", label + " must be an object.")
    if set(value) - allowed:
        # Do not echo arbitrary values or credential-like field names into logs.
        raise Fault("CAPABILITY_UNSUPPORTED", label + " contains unsupported fields.")
    if required - set(value):
        raise Fault("INVALID_INPUT", label + " is missing required explicit fields.")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise Fault("INVALID_INPUT", label + " must be an explicit bounded token.")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise Fault("INVALID_INPUT", label + " must be a positive bounded integer.")
    return value


def _booleans(value: Any, names: set[str], label: str) -> None:
    _object(value, names, names, label)
    if any(type(value[key]) is not bool for key in names):
        raise Fault("INVALID_INPUT", label + " requires explicit Boolean values.")


def _validate_codex_quality(quality: dict) -> None:
    _object(quality, _CODEX_QUALITY, {"model_reasoning_effort"}, "Codex quality")
    # Recent Codex accepts model-advertised strings, including max / ultra.
    # A token is not evidence that the selected model supports that level.
    _token(quality["model_reasoning_effort"], "model_reasoning_effort")
    if "model_reasoning_summary" in quality:
        value = quality["model_reasoning_summary"]
        if not isinstance(value, str) or value not in {"auto", "concise", "detailed"}:
            raise Fault("CAPABILITY_UNSUPPORTED", "Only explicit auto, concise, or detailed reasoning summaries have a request mapping; none is not verified.")
    if "model_verbosity" in quality:
        value = quality["model_verbosity"]
        if not isinstance(value, str) or value not in {"low", "medium", "high"}:
            raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported model_verbosity.")
    if "service_tier" in quality:
        _token(quality["service_tier"], "service_tier")
    for name in ("model_context_window", "model_auto_compact_token_limit"):
        if name in quality:
            _positive_integer(quality[name], name)


def _validate_codex_catalog(snapshot: dict) -> None:
    """Preserve a supplied native catalog; never infer or synthesize metadata.

    The selected entry must declare the requested reasoning capability and
    fundamental context/tool metadata. Native Codex still validates the complete
    version-specific ModelInfo schema when the immutable catalog is loaded.
    """
    catalog = snapshot["codex_model_catalog"]
    _object(catalog, {"models"}, {"models"}, "Codex model catalog")
    if len(canonical_json(catalog).encode("utf-8")) > MAX_CODEX_CATALOG_BYTES:
        raise Fault("INPUT_TOO_LARGE", "Codex model catalog exceeds one MiB.")
    models = catalog["models"]
    if not isinstance(models, list) or not 1 <= len(models) <= 64:
        raise Fault("INVALID_INPUT", "Codex catalog requires one to 64 explicit entries.")
    seen, selected = set(), None
    for entry in models:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str) or not _MODEL.fullmatch(entry["slug"]):
            raise Fault("INVALID_INPUT", "Every Codex catalog entry requires an exact model slug.")
        if entry["slug"] in seen:
            raise Fault("INVALID_INPUT", "Codex catalog model slugs must be unique.")
        seen.add(entry["slug"])
        if entry["slug"] == snapshot["model"]:
            selected = entry
    if selected is None:
        raise Fault("CAPABILITY_UNSUPPORTED", "Codex catalog has no exact selected model entry.")
    required = {"slug", "display_name", "base_instructions", "supported_reasoning_levels",
                "shell_type", "visibility", "supported_in_api", "priority", "support_verbosity",
                "context_window", "truncation_policy", "experimental_supported_tools"}
    if required - set(selected):
        raise Fault("INVALID_INPUT", "Selected Codex catalog entry omits required explicit metadata.")
    for key in ("display_name", "base_instructions", "shell_type", "visibility"):
        if not isinstance(selected[key], str) or not selected[key]:
            raise Fault("INVALID_INPUT", "Selected Codex catalog string metadata is invalid.")
    if type(selected["supported_in_api"]) is not bool or type(selected["support_verbosity"]) is not bool:
        raise Fault("INVALID_INPUT", "Selected Codex catalog capability metadata requires Booleans.")
    if not selected["supported_in_api"]:
        raise Fault("CAPABILITY_UNSUPPORTED", "Selected Codex catalog model is not declared API-compatible.")
    if type(selected["priority"]) is not int:
        raise Fault("INVALID_INPUT", "Selected Codex catalog priority requires an integer.")
    _positive_integer(selected["context_window"], "Codex catalog context_window")
    truncation = _object(selected["truncation_policy"], {"mode", "limit"}, {"mode", "limit"}, "Codex truncation policy")
    if not isinstance(truncation["mode"], str) or truncation["mode"] not in {"bytes", "tokens"}:
        raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported Codex catalog truncation mode.")
    _positive_integer(truncation["limit"], "Codex catalog truncation limit")
    tools = selected["experimental_supported_tools"]
    if not isinstance(tools, list) or len(tools) > 128 or any(not isinstance(item, str) or not item for item in tools):
        raise Fault("INVALID_INPUT", "Selected Codex catalog tools must be bounded explicit names.")
    levels = selected["supported_reasoning_levels"]
    if not isinstance(levels, list) or len(levels) > 32:
        raise Fault("INVALID_INPUT", "Selected Codex catalog reasoning levels must be explicit.")
    efforts = set()
    for level in levels:
        _object(level, {"effort", "description"}, {"effort", "description"}, "Codex reasoning level")
        effort = _token(level["effort"], "Codex catalog reasoning effort")
        if effort in efforts or not isinstance(level["description"], str):
            raise Fault("INVALID_INPUT", "Codex catalog reasoning levels must be unique descriptions.")
        efforts.add(effort)
    if snapshot["quality"]["model_reasoning_effort"] not in efforts:
        raise Fault("CAPABILITY_UNSUPPORTED", "Selected catalog entry does not declare the exact requested reasoning effort.")
    if "model_verbosity" in snapshot["quality"] and not selected["support_verbosity"]:
        raise Fault("CAPABILITY_UNSUPPORTED", "Selected catalog entry does not declare verbosity support.")


def _validate_zcode_quality(snapshot: dict) -> None:
    quality = snapshot["quality"]
    required = {"reasoningLevel", "maxOutputTokens"}
    allowed = set(required)
    if snapshot["api"] == "anthropic-messages":
        required.add("thinking")
        allowed.update({"thinking", "output_config"})
    _object(quality, allowed, required, "ZCode quality")
    _token(quality["reasoningLevel"], "reasoningLevel")
    _positive_integer(quality["maxOutputTokens"], "maxOutputTokens")
    if "thinking" in quality:
        thinking = _object(quality["thinking"], {"type", "budget_tokens"}, {"type"}, "thinking")
        if not isinstance(thinking["type"], str) or thinking["type"] not in {"enabled", "disabled", "adaptive"}:
            raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported thinking type.")
        if "budget_tokens" in thinking:
            if thinking["type"] != "enabled":
                raise Fault("CAPABILITY_UNSUPPORTED", "A thinking budget is supported only for explicit enabled thinking.")
            _positive_integer(thinking["budget_tokens"], "budget_tokens")
    if "output_config" in quality:
        output = _object(quality["output_config"], {"effort"}, {"effort"}, "output_config")
        _token(output["effort"], "output_config.effort")
    properties = _object(snapshot["zcode_model_properties"], _ZCODE_PROPERTIES,
                         _ZCODE_PROPERTIES, "ZCode model properties")
    _positive_integer(properties["contextWindow"], "contextWindow")
    _booleans(properties["inputFormat"], _ZCODE_INPUT, "inputFormat")
    _booleans(properties["outputFormat"], {"supportsText"}, "outputFormat")
    for name in _ZCODE_PROPERTIES - {"contextWindow", "inputFormat", "outputFormat"}:
        if type(properties[name]) is not bool:
            raise Fault("INVALID_INPUT", "Model capability values must be explicit Booleans.")


def normalize_host_snapshot(snapshot: Any) -> dict:
    """Validate and copy an explicit, credential-free owner snapshot.

    No defaults are inserted, model aliases resolved, existing files read, or
    missing model capabilities inferred. Unknown fields fail closed.
    """
    value = load_json(canonical_json(snapshot), limit=MAX_CODEX_CATALOG_BYTES + 65_536)
    base = {"schema_version", "engine", "provider_id", "api", "model", "quality"}
    _object(value, base | {"zcode_model_properties", "codex_model_catalog"}, base, "Host snapshot")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported host snapshot schema version.")
    if value["engine"] not in ("codex", "zcode"):
        raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported host engine.")
    validate_identifier(value["provider_id"], "registered provider id")
    if not isinstance(value["api"], str) or value["api"] not in _APIS:
        raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported provider API.")
    if not isinstance(value["model"], str) or not _MODEL.fullmatch(value["model"]):
        raise Fault("INVALID_INPUT", "Model must be an exact bounded identifier, without whitespace or control characters.")
    if not isinstance(value["quality"], dict):
        raise Fault("INVALID_INPUT", "Quality must be an explicit object.")
    if value["engine"] == "codex":
        if value["api"] != "openai-responses":
            raise Fault("CAPABILITY_UNSUPPORTED", "This Codex adapter supports only the Responses wire API.")
        if "zcode_model_properties" in value:
            raise Fault("CAPABILITY_UNSUPPORTED", "ZCode model properties cannot be applied to Codex.")
        _validate_codex_quality(value["quality"])
        if "codex_model_catalog" in value:
            _validate_codex_catalog(value)
    else:
        if "codex_model_catalog" in value:
            raise Fault("CAPABILITY_UNSUPPORTED", "A Codex model catalog cannot be applied to ZCode.")
        if "zcode_model_properties" not in value:
            raise Fault("INVALID_INPUT", "ZCode requires every model capability explicitly.")
        _validate_zcode_quality(value)
    return value


def derive_request_constraints(snapshot: Any) -> dict:
    """Return exact supplied request fields for broker enforcement, incl model.

    These are required nested values, not a replacement request body. In-memory
    CLI context/compaction settings are preserved in config but have no wire key.
    """
    value = normalize_host_snapshot(snapshot)
    quality = value["quality"]
    result: dict[str, Any] = {"model": value["model"]}
    if value["engine"] == "codex":
        result["reasoning"] = {"effort": quality["model_reasoning_effort"]}
        if "model_reasoning_summary" in quality:
            result["reasoning"]["summary"] = quality["model_reasoning_summary"]
        if "model_verbosity" in quality:
            result["text"] = {"verbosity": quality["model_verbosity"]}
        if "service_tier" in quality:
            tier = quality["service_tier"]
            result["service_tier"] = "priority" if tier == "fast" else tier
    elif value["api"] == "openai-responses":
        result.update(reasoning={"effort": quality["reasoningLevel"]},
                      max_output_tokens=quality["maxOutputTokens"])
    elif value["api"] == "openai-chat-completions":
        result.update(reasoning_effort=quality["reasoningLevel"],
                      max_completion_tokens=quality["maxOutputTokens"])
    else:
        result.update(thinking=quality["thinking"], max_tokens=quality["maxOutputTokens"])
        if "output_config" in quality:
            result["output_config"] = quality["output_config"]
    return result


def _codex_configuration(value: dict, base_url: str, capability: str) -> tuple[list, dict, list]:
    top: dict[str, Any] = {"model": value["model"], "model_provider": BRIDGE_PROVIDER_ID,
                           **value["quality"], "web_search": "disabled"}
    if "codex_model_catalog" in value:
        top["model_catalog_json"] = PRIVATE_HOME + "/" + CODEX_CATALOG_PATH
    provider = {"name": "Xiaolvji broker bridge", "base_url": base_url,
                "env_key": CAPABILITY_ENV, "wire_api": "responses",
                "requires_openai_auth": False, "supports_websockets": False}
    # All string values are bounded ASCII: JSON scalar syntax is also valid
    # TOML scalar syntax, without untrusted table names or raw TOML fragments.
    lines = [f"{key} = {canonical_json(item)}" for key, item in sorted(top.items())]
    lines += ["", f"[model_providers.{BRIDGE_PROVIDER_ID}]"]
    lines += [f"{key} = {canonical_json(item)}" for key, item in sorted(provider.items())]
    argv = []
    # Pin model/quality/transport and disable provider-hosted web search, which
    # the broker deliberately does not authorize. This capability restriction
    # does not change model quality, trust, sandbox, rules, approvals, or managed
    # requirements. No raw config import.
    pinned = list(sorted(top.items())) + [
        (f"model_providers.{BRIDGE_PROVIDER_ID}.{key}", item) for key, item in sorted(provider.items())
    ]
    for key, item in pinned:
        argv.extend(["-c", f"{key}={canonical_json(item)}"])
    files = [{"relative_path": CODEX_CONFIG_PATH, "content": "\n".join(lines) + "\n", "mode": 0o600}]
    if "codex_model_catalog" in value:
        files.append({"relative_path": CODEX_CATALOG_PATH,
                      "content": canonical_json(value["codex_model_catalog"]) + "\n", "mode": 0o600})
    env = {"CODEX_HOME": PRIVATE_HOME + "/.codex", CAPABILITY_ENV: capability}
    return files, env, argv


def _zcode_configuration(value: dict, base_url: str, capability: str) -> tuple[list, dict, list]:
    quality = value["quality"]
    if value["api"] == "openai-responses":
        reasoning_map = '{"reasoning":{"effort":reasoningLevel}}'
        maximum_map = '{"max_output_tokens":maxOutputTokens}'
    elif value["api"] == "openai-chat-completions":
        reasoning_map = '{"reasoning_effort":reasoningLevel}'
        maximum_map = '{"max_completion_tokens":maxOutputTokens}'
    else:
        fields = {"thinking": quality["thinking"]}
        if "output_config" in quality:
            fields["output_config"] = quality["output_config"]
        reasoning_map = canonical_json(fields)
        maximum_map = '{"max_tokens":maxOutputTokens}'
    # A single exact model rule prevents inheriting an unreviewed model catalog.
    # Escape only JavaScript regex metacharacters; model has no whitespace.
    model_match = "^" + re.sub(r"([\\^$.*+?()\[\]{}|])", r"\\\1", value["model"]) + "$"
    builtin = {"schemaVersion": 1, "revision": 1, "config": {
        "providerConfigRules": {"providerRules": [], "templateRules": []},
        "modelConfigRules": {
            "modelRules": [{"modelMatch": model_match, "config": {
                "enabled": True, "properties": value["zcode_model_properties"],
                "optionSpecs": {
                    "reasoningLevel": {"values": [quality["reasoningLevel"]], "map": reasoning_map},
                    "maxOutputTokens": {"max": quality["maxOutputTokens"], "map": maximum_map},
                },
            }}],
            "modelApiRules": [], "providerSiteRules": [], "templateModelRules": [],
            "builtinProviderModelRules": [],
        },
    }}
    personal = {"schemaVersion": 1, "config": {
        "providerConfigRules": {"providerRules": [{
            "providerId": BRIDGE_PROVIDER_ID, "providerName": "Xiaolvji broker bridge",
            "config": {"group": "standard-personal",
                       "access": {"type": "api-key", "apiKey": capability},
                       "api": {"type": value["api"], "baseUrl": base_url},
                       "personalModelIds": [value["model"]]},
        }]},
        "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
        "defaultModelSelection": {"providerId": BRIDGE_PROVIDER_ID, "modelId": value["model"],
                                  "options": {"reasoningLevel": quality["reasoningLevel"],
                                              "maxOutputTokens": quality["maxOutputTokens"]}},
    }}
    files = [{"relative_path": path, "content": canonical_json(config) + "\n", "mode": 0o600}
             for path, config in ((ZCODE_PERSONAL_PATH, personal), (ZCODE_BUILTIN_PATH, builtin))]
    env = {"ZCODE_DATA_BASE_DIR": PRIVATE_HOME,
           "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": PRIVATE_HOME + "/" + ZCODE_PERSONAL_PATH,
           "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": PRIVATE_HOME + "/" + ZCODE_BUILTIN_PATH}
    # Pinned ZCode has no --model or generic --settings switch.
    return files, env, []


def render_host_config(snapshot: Any, bridge_url: str, capability: str,
                       home: str = PRIVATE_HOME) -> dict:
    """Render immutable private-HOME data for a controller-issued capability.

    The URL is an origin, exactly http://127.0.0.1:<port>; /v1 is added here.
    Capability provenance must be verified by the caller. Syntax cannot tell a
    broker-issued token from an upstream secret with the same character set.
    """
    value = normalize_host_snapshot(snapshot)
    if home != PRIVATE_HOME:
        raise Fault("CAPABILITY_UNSUPPORTED", "Only the fixed isolated HOME is supported.")
    match = _BRIDGE_ORIGIN.fullmatch(bridge_url) if isinstance(bridge_url, str) else None
    if match is None or int(match.group(1)) > 65535:
        raise Fault("INVALID_INPUT", "Bridge URL must be an explicit IPv4 loopback HTTP origin with a valid port.")
    if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
        raise Fault("INVALID_INPUT", "A bounded URL-safe per-attempt broker capability is required.")
    render = _codex_configuration if value["engine"] == "codex" else _zcode_configuration
    files, env, argv = render(value, bridge_url + "/v1", capability)
    evidence = {"runtime_verified": False, "real_model_request_tested": False}
    if value["engine"] == "codex":
        evidence.update(level="configuration_rendered_native_acceptance_separate",
                        observed_cli_version="0.167.0-alpha.4",
                        observed_on="2026-11-09",
                        limitation="Rendering is not native readiness. The standalone App Server profile requires a fresh policy-bound controller acceptance record.")
    else:
        evidence.update(level="source_verified_execution_unverified",
                        source_commit=ZCODE_SOURCE_COMMIT,
                        limitation="Pinned complete ZCode source and native option mapping were verified separately; a complete CLI build and provider-schema load are still unverified.")
    return {"engine": value["engine"], "provider_id": value["provider_id"],
            "files": files, "env": env, "argv_extra": argv,
            "request_constraints": derive_request_constraints(value),
            "snapshot_digest": digest(value),
            "config_digest": digest({"files": files, "env": env, "argv_extra": argv}),
            "evidence": evidence}
