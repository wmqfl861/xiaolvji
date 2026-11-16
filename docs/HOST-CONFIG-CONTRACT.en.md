# Explicit host configuration contract

Updated for the 0.5 development work on 2026-11-16 (UTC).

This module renders an owner-approved model snapshot into private per-attempt
configuration. It does not discover the user's configuration, read credentials,
log in, select a cheaper model, or establish that a host can execute a real job.
`normalize_host_snapshot`, `derive_request_constraints`, and
`render_host_config` are pure functions in
[xiaolvji.host_config](../src/xiaolvji/host_config.py). Rendering is separate from
native protocol acceptance and production policy eligibility. The new Codex
App Server fixture has passed real native tool and boundary checks; this
machine's managed policy still blocks production standalone admission. See
[CODEX-COMPATIBILITY.en.md](CODEX-COMPATIBILITY.en.md) and
[NATIVE-HOST-CONTRACT.en.md](NATIVE-HOST-CONTRACT.en.md).

The controller must bind the snapshot's `provider_id`, API, exact model, and
derived request constraints to one registered broker profile before starting an
attempt. The ID identifies that external registration. `xiaolvji_bridge` is an
internal host transport alias; it does not authorize a different upstream
provider. An untrusted worker cannot register or change this binding.

## Input and no-fallback rules

An example Codex snapshot is:

```json
{
  "schema_version": 1,
  "engine": "codex",
  "provider_id": "owner-provider-registration",
  "api": "openai-responses",
  "model": "owner-specified-model-id",
  "quality": {
    "model_reasoning_effort": "ultra",
    "model_reasoning_summary": "detailed",
    "model_verbosity": "high",
    "service_tier": "fast",
    "model_context_window": 200000,
    "model_auto_compact_token_limit": 150000
  }
}
```

This is a structural example, not a claim that a particular provider/model
supports these values. The explicit snapshot is copied, with no missing defaults
inserted. Top-level, quality, and ZCode model-property fields form closed schemas.
Raw TOML, arbitrary environment variables, headers, auth commands, fallback
models, credentials, and unsupported settings fail closed. Complete entries in
the optional native Codex catalog retain their version-specific native metadata;
the native loader must validate that schema as described below.
Model IDs, provider IDs, reasoning labels, and numbers are bounded; values are
not trimmed, case-folded, or aliased. Unrecognized future settings require a
reviewed adapter extension.

This API is not an importer of an existing user's settings. The owner must
explicitly supply every quality setting they require. Omitted optional settings
cannot be described as preserving an unseen original configuration. The caller
must reject an incomplete snapshot if its required settings cannot be expressed.

## Codex mapping

Codex accepts only the `openai-responses` API in this adapter. Native quality
fields and their required request values are:

| Snapshot quality field | Required or optional | Broker request constraint |
| --- | --- | --- |
| `model_reasoning_effort` | Required explicit string | `reasoning.effort`, unchanged |
| `model_reasoning_summary` | Optional `auto`, `concise`, or `detailed` | `reasoning.summary`, unchanged |
| `model_verbosity` | Optional `low`, `medium`, or `high` | `text.verbosity`, unchanged |
| `service_tier` | Optional explicit string | `fast` becomes `priority`; other strings remain unchanged |
| `model_context_window` | Optional positive integer | No request field; native `config/read` must return the exact supplied value and type |
| `model_auto_compact_token_limit` | Optional positive integer | No request field; native `config/read` must return the exact supplied value and type |

The current official configuration reference describes model-advertised reasoning
strings, the summary/verbosity values, custom Responses providers, and the
`fast` to `priority` conversion. This adapter does not replace a model's
advertised effort with an older fixed enumeration. See the
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

`model_reasoning_summary="none"` is deliberately blocked: its wire omission
behavior has not been verified with the current host, and this contract does not
silently convert it to another setting. There is no supported
`model_max_output_tokens` configuration key in the local strict-load probe.

The rendered file is `.codex/config.toml`, relative to the fixed private HOME.
It sets the explicit model, quality, and a custom provider with
`wire_api="responses"`, the private bridge URL, a capability environment key,
`requires_openai_auth=false`, and `supports_websockets=false`. It also pins
`web_search="disabled"`: provider-hosted search is outside the broker's allowed
tool surface. This is an explicit capability restriction, not a substitute model
or a lower reasoning setting. These authentication
settings apply to the new broker transport; they do not copy or change the
owner's provider configuration. The external broker owns upstream authentication.

`argv_extra` repeats the model, supplied quality values, provider transport,
disabled web search, and optional catalog path as `-c key=value` pairs. The
ordinary CLI adapter inserts these before its stdin sentinel. The verified
App Server runner passes the same validated arguments to `app-server
--strict-config`. Each override must contain exactly one supported TOML value;
extra settings and raw configuration fragments are rejected. The renderer does
not set approval, trust, permission, sandbox, rules, or Hook switches.

Before a turn, the native runner compares the selected model/provider and every
supplied supported quality setting against effective `config/read` values,
including the two controls with no HTTP representation. It then requires the
exact model and effort in `model/list`. A host that cannot report or support the
required values fails; the broker alone cannot prove offline context behavior.
Native warnings, including a generic metadata fallback, are not accepted as a
successful result. See
[codex_app_server.py](../src/xiaolvji/codex_app_server.py) and its
`tests/test_codex_app_server.py` (full source delivery).

Applicable managed policy is a separate prerequisite. The initial standalone
deployment profile requires absence of the system policy root and rejects
unsupported project policy. App Server must additionally report
`requirements: null`; nonempty requirements fail before a provider request.
Rendering a private HOME does not transfer, preserve, or authorize omission of
an existing environment's policy. This machine has `/etc/codex`, so production
admission remains blocked despite passing isolated synthetic native fixtures.

### Optional exact native model catalog

A Codex snapshot may include `codex_model_catalog`, containing the native
ModelsResponse shape `{"models": [...]}`. Its complete selected entry must come
from explicit owner-supplied model metadata. There is no model-name heuristic,
automatic alias, or synthesized generic metadata. The optional field is rejected
for ZCode. Official
[gateway guidance](https://learn.chatgpt.com/docs/enterprise/roll-out-a-gateway)
documents `model_catalog_json` for custom model identifiers.

The renderer validates a bounded subset before preserving the complete catalog:

- The catalog is at most one MiB of canonical JSON and contains 1–64 entries
  with unique exact slugs. Exactly one entry matches the selected model.
- The selected entry explicitly supplies `slug`, `display_name`,
  `base_instructions`, `supported_reasoning_levels`, `shell_type`, `visibility`,
  `supported_in_api`, `priority`, `support_verbosity`, `context_window`,
  `truncation_policy`, and `experimental_supported_tools`.
- Types and bounds must be valid; API compatibility must be true. The declared
  reasoning levels must include the exact requested effort. A supplied verbosity
  setting requires declared verbosity support.

This subset check does not replace Codex's complete version-specific ModelInfo
validation. Remaining native metadata is preserved unchanged; the selected
executable must load the catalog and report the exact model through `model/list`.
If the model is already present in the native catalog, the optional owner catalog
is not required. An unknown model must not proceed with generic fallback data.

The catalog becomes the immutable private-HOME file
`.codex/model-catalog.json`. Both generated TOML and CLI arguments select the
fixed absolute path `/home/xiaolvji/.codex/model-catalog.json`. Catalog changes
change the snapshot/configuration digest and invalidate an earlier native
acceptance binding. The renderer does not change catalog instructions, tool
capabilities, context limits, or reasoning declarations to force a probe to pass.

## ZCode mapping

ZCode has no supported generic `--model`, `--settings`, `ZCODE_MODEL`, or
`ZCODE_API_KEY` interface in the pinned source used here. Configuration uses
explicit builtin and personal provider files. The personal file's
`defaultModelSelection` holds the exact internal provider, model, and options;
`argv_extra` is empty.

The required `quality` fields are `reasoningLevel` and `maxOutputTokens`.
Anthropic additionally requires an explicit `thinking` object. No reasoning
budget is subtracted from the configured output budget.

| API | Required native request fields |
| --- | --- |
| `openai-responses` | `reasoning.effort = reasoningLevel`; `max_output_tokens = maxOutputTokens` |
| `openai-chat-completions` | `reasoning_effort = reasoningLevel`; `max_completion_tokens = maxOutputTokens` |
| `anthropic-messages` | Exact supplied `thinking`; `max_tokens = maxOutputTokens`; exact supplied `output_config` when present |

OpenAI mappings support direct effort labels. Existing model definitions that map
a label to a different wire representation cannot be imported by pretending they
use this mapping. Anthropic's `thinking.type` supports `enabled`, `disabled`, or
`adaptive`; an explicit positive `budget_tokens` is accepted only with `enabled`.
The optional `output_config` permits only an explicit `effort` string. Server-side
support and inter-field validity are not inferred from structural validation.

ZCode also requires `zcode_model_properties` with every field below:

```json
{
  "contextWindow": 200000,
  "requiresMfjsToolSchema": false,
  "inputFormat": {
    "supportsText": true,
    "supportsImage": false,
    "supportsVideo": false,
    "supportsAudio": false,
    "supportsPdf": false
  },
  "outputFormat": {"supportsText": true},
  "supportsToolCall": true,
  "supportsJsonSchemaOutput": false,
  "supportsNativeWebSearch": false,
  "supportsMidConversationSystem": false
}
```

These example capabilities must be replaced by the owner's verified model facts.
The renderer preserves every Boolean, including `false`; it never chooses them
from a model-name heuristic. A single anchored model rule supplies these exact
properties. Its option map permits the selected reasoning label and caps the
attempt at the exact configured output budget. Other catalogs and model rules
are empty. Arbitrary option-map expressions cannot be supplied by the caller.

This uses the source-demonstrated builtin `modelRules` format. The complete
pinned checkout also contains the provider and model-property schemas; earlier
missing paths were a sparse-checkout artifact, not missing public dependencies.
This renderer does not import arbitrary populated `manualProviderModelRules` or
caller-defined option expressions. The
personal provider references the one selected model and contains only the
per-attempt synthetic capability as `access.apiKey`. Literal environment-variable
interpolation is not assumed. Both files must be immutable: rewriting a default
selection, catalog, or provider at runtime is not permitted by this contract.

## Renderer and sandbox boundary

```python
snapshot = normalize_host_snapshot(explicit_owner_snapshot)
constraints = derive_request_constraints(snapshot)
rendered = render_host_config(snapshot, bridge_url, broker_issued_capability)
```

`bridge_url` must be exactly `http://127.0.0.1:<port>`, without a trailing slash,
path, credentials, query, or fragment. The renderer appends `/v1`. DNS names,
other loopback addresses, IPv6, and non-loopback hosts are rejected. The caller
must start the bridge in the attempt's private network namespace; URL syntax
alone does not prove where a listener runs.

The HOME is fixed at `/home/xiaolvji`; another HOME is rejected. The renderer
returns file **data**, not host paths to mount. Every returned entry has
`relative_path`, UTF-8 `content`, and `mode=384` (`0600`). The sandbox should mount
sealed read-only data at these exact paths and reject symlinks, traversal,
duplicates, unknown destinations, and arbitrary environment injection.

| Engine | Allowed relative files | Exact environment keys |
| --- | --- | --- |
| Codex | `.codex/config.toml`; optional `.codex/model-catalog.json` | `CODEX_HOME`, `XIAOLVJI_PROVIDER_CAPABILITY` |
| ZCode | `.zcode/v2/provider_config.json`; `.zcode/v2/xiaolvji_builtin_config.json` | `ZCODE_DATA_BASE_DIR`, `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`, `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` |

`HOME` itself remains owned by the sandbox. No ambient environment is merged.
The controller separately seals `.xiaolvji/codex_app_server.py` when selecting
the verified App Server protocol. That executable is controller-owned runtime
code, not a caller-provided renderer destination.

The capability is an opaque 32–256 character URL-safe string. Syntax cannot prove
that it is synthetic: the caller must accept only the broker's authenticated,
attempt-scoped issuance and must never pass an upstream key here.

`snapshot_digest` binds the normalized credential-free snapshot. `config_digest`
binds the exact files, environment, and `argv_extra`, including the capability;
it changes when the capability rotates. Record digests and evidence, not raw
renderer output. File content and environment must not enter journals, command
logs, exception payloads, or release artifacts.

The returned `request_constraints` includes `model`. A broker with a separate
model field must compare that field exactly and use the remaining members as
native required request constraints. Missing or changed constraints must reject
the request; the broker must not repair them, choose another model, or lower
reasoning. CLI-only context fields require the native effective-configuration
check described above. Host turn events are not proof of process-tree exit or
task acceptance.

The Responses broker accepts native `client_metadata` only as opaque bounded
data: at most 32 string entries, keys of 1–128 UTF-8 bytes, values of at most
4096 UTF-8 bytes, and at most 32768 canonical JSON bytes in total. Nested objects
and non-string values are rejected. This field never changes endpoint, model,
quality, capability, or tool authorization. Other request allowlists and the
prohibition on provider-hosted tools remain in effect. Deferred `tool_search`
observed in an MCP diagnostic is unsupported and rejected. See
[provider_broker.py](../src/xiaolvji/provider_broker.py).

## Evidence and unresolved host boundaries

The installed `codex-cli 0.167.0-alpha.4` passed strict loading of the generated
provider/model/quality configuration and rejection of an unknown output-budget
key. The `XIAOLVJI_CODEX_CLI_SMOKE` opt-in still tests loading only. It does not
start a turn and cannot establish native tool execution.

The separate `XIAOLVJI_CODEX_NATIVE_SMOKE` suite now runs six actual native cases:
the production App Server runner with all 13 acceptance checks; all six supplied
quality settings; `exec_command` plus `write_stdin`; cancellation; preservation
and rejection of real managed requirements; and unknown-model rejection. All
passed on the observed machine using fixed local fixtures and synthetic
credentials. No external model or authenticated provider was contacted. Ordinary
nested native workspace-write sandboxing still fails at the shell-tool stage;
the passing path uses documented `externalSandbox` inside the unchanged strict
executor. Exact observations and commands are in
[CODEX-COMPATIBILITY.en.md](CODEX-COMPATIBILITY.en.md).

ZCode evidence has advanced from inspection to execution of a small verified
closure of the actual pinned source. Its option-map compiler/evaluator,
merge-patch logic, native agent aliases, and environment sanitizer executed
against renderer-generated synthetic inputs. The 0.5 probe also executes the exact
Git-bound native argument extractor/parser on seven production command cases,
including option-like prompts and both 120,000-byte prompt boundaries. All eleven
executed source files are bound to their pinned Git blobs. The complete checkout contains the
provider/schema/model-option-map packages. The earlier contrary conclusion was
caused by a sparse checkout. Full CLI dependencies remain uninstalled: a clean
fetch of the required pinned pnpm package failed with
`EAI_AGAIN registry.npmjs.org`. This is an environment/dependency-installation
blocker, not a claim that the public source is incomplete.

The native source diagnostic does not establish full schema loading, SDK request
construction, a built or installed ZCode CLI, a native agent turn, or Hook/MCP
trust. The inspected CLI loads workspace `.env` with `override: false`; its
provider preparation preserves the supplied configuration paths, and builtin
remote refresh requires an additional bundled-source path that Xiaolvji does
not supply. Complete-launch behavior and immutable-file compatibility still
need actual CLI acceptance. ZCode production native jobs remain blocked. See
[ZCODE-COMPATIBILITY.en.md](ZCODE-COMPATIBILITY.en.md).

For both engines, the pure renderer still returns
`evidence.runtime_verified: false` and `evidence.real_model_request_tested: false`.
Those fields describe what rendering proves. Separate native fixture evidence
does not turn a render result into a production-readiness certificate, approval,
authenticated-provider result, or final task-quality acceptance.

### Pinned ZCode primary sources

All links below refer to commit `a60ba6a7e5a06742270ca605f5d2cc2766afa7e1`:

- [Personal provider format and settings](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/03-config.md).
- [CLI flags and unsupported aliases](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/02-cli-reference.md).
- [Explicit provider-file environment handling](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/provider-runtime-env.ts).
- [Complete builtin model-rule fixture](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/bootstrap/tests/process-provider-registry-runtime.test.ts).
- [Provider rule schema in the complete checkout](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/packages/provider/src/config/rule-data-schema.ts).
- [Model property and option schemas](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/packages/shared/src/model-config.ts).
- [Executed native option-map compiler and evaluator](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/packages/model-option-map/src/option-maps.ts).
- [Native option-map request tests](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/tests/model-option-map-fetch.test.ts).
- [Final request option-map implementation](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/model/model-option-map-fetch.ts).
- [API construction, headers, and Anthropic base-URL handling](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/model/model-execution.ts).
- [Workspace environment-file loading](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/env.ts).
