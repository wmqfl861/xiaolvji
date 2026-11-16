# Local Codex compatibility evidence

Updated for the 0.5 development work on 2026-11-16 (UTC). The inspected executable,
`/opt/codex/bin/codex`, reports `codex-cli 0.167.0-alpha.4`. Its App Server now
completes a deterministic native tool turn inside Xiaolvji's actual strict
executor. The provider is a local HTTP/SSE fixture with synthetic credentials;
this is native protocol and boundary evidence, not an authenticated external
model run or a model-quality benchmark.

**This machine remains ineligible for production standalone native admission.**
Its real `/etc/codex` policy root exists. The initial deployment profile requires
that root to be absent, and managed-policy transfer is not implemented. A
separate negative test preserves the real policy root read-only and verifies
rejection before any provider request. The isolated positive fixtures do not
change that production decision.

Results apply to this installed binary, the tested model metadata, and this Linux
environment. A version string, copied report, or configuration-rendering result
cannot enable another deployment. Its controller must establish and recheck its
own acceptance record. See [NATIVE-HOST-CONTRACT.en.md](NATIVE-HOST-CONTRACT.en.md).

## What changed in 0.4

| Path or check | Observed result | Scope of the evidence |
| --- | --- | --- |
| Version/help and strict configuration loading | Passed; invalid configuration controls rejected | Recognized CLI arguments and configuration fields. |
| Ordinary `codex exec --sandbox workspace-write` inside the strict executor | A synthetic text turn can reach the provider; a native shell tool fails because nested bubblewrap cannot create a namespace | This remains an incompatible native tool-execution path here. |
| App Server with `externalSandbox`, inside the unchanged outer strict executor | Complete native tool turn passed | Actual native tool execution, streaming, and scoped filesystem/network enforcement for the fixture. |
| Fixed native acceptance probe | All 13 checks passed in the synthetic standalone fixture | The tested executable, exact host snapshot, runner, transport, and outer boundary work together. |
| Installed-Codex opt-in suite | Six actual native cases passed | Tool loops, exact supplied quality settings, cancellation, managed-policy rejection, and unknown-model rejection. |
| Production standalone admission on this machine | Blocked by the real system policy root | Synthetic isolation cannot erase production policy obligations. |
| Authenticated external provider and model-quality acceptance | Not attempted | Separate deployment and task-quality evidence is required. |
| Installed plugin and native Hook trust/event coverage | Not established | The fixture does not install or approve a plugin or Hook. |

Executable cases are in `tests/test_codex_app_server.py` (full source delivery).
The fixed checks are in [native_acceptance.py](../src/xiaolvji/native_acceptance.py).

## 0.5 input-boundary correction

The App Server runner now checks timeout type and its existing 1–86,400 second
range before calling a floating-point finite check. A bounded JSON integer such
as `10**400` is rejected as `NATIVE_INPUT_INVALID` through the typed error and
failed-turn output; it cannot trigger an uncaught numeric overflow or start a
native process. This does not change allowed models, quality, permission or
managed-policy gates.

## Verified execution path

The original adapter still represents the ordinary CLI command:

```text
codex exec --json --sandbox workspace-write --cd WORKSPACE -c agents.enabled=false -
```

That base CLI contract is separate from the verified 0.4 native runner. For the
`codex-app-server-v2` deployment protocol, the controller copies its exact
stdlib-only [codex_app_server.py](../src/xiaolvji/codex_app_server.py) into an
immutable private-HOME file and invokes it with Python `-I -S`. A bounded, closed
JSON envelope on stdin supplies the pinned executable path, workspace, prompt,
exact model and effort, and renderer-generated configuration arguments. It starts:

```text
ABSOLUTE_CODEX_PATH app-server --stdio --strict-config -c agents.enabled=false [RENDERED_CONFIG_ARGUMENTS]
```

The existing controlled executor owns the mount, namespace, seccomp, network,
and process-tree boundary. The runner is not itself an OS sandbox. The supported
App Server `externalSandbox` policy is used inside that enforced boundary, with
`networkAccess: restricted`. See the official
[App Server documentation](https://learn.chatgpt.com/docs/app-server) and
[permissions documentation](https://learn.chatgpt.com/docs/permissions).

The protocol proceeds through these checks:

1. Initialize App Server and call `configRequirements/read`. The initial
   standalone profile requires explicit `requirements: null`; missing or
   nonempty requirements fail before a model turn.
2. Call `config/read` for the exact workspace. Verify the effective model,
   provider, disabled web search, and every supplied supported quality setting.
   Values and types must match, including context-window and automatic
   compaction limits that never appear in provider requests. An incompatible
   approval policy or reviewer also fails.
3. Call `model/list` with `includeHidden: true` and bounded pagination. Require
   exactly one exact model and its declared requested effort. Unknown metadata,
   duplicate matches, and unsupported effort fail without substitution.
4. Start an ephemeral thread with the exact model/provider/workspace and
   `workspace-write`. Verify its resolved settings, including
   `approvalPolicy: on-request` and `approvalsReviewer: user`. Start the turn
   with `externalSandbox` and verify the settings notification before accepting
   `turn/started`.
5. Match thread and turn identities. Reject native delegation items,
   warnings/errors, malformed or ambiguous messages, and unsuccessful terminals.
   Native approval requests are declined; other interactive requests receive an
   error. Neither case becomes a successful attempt.

The runner never supplies an approval-policy bypass or auto-accepts a native
request. Externally sandboxed execution nevertheless has different native
approval/rule semantics from workspace-write execution. In a synthetic probe,
an escalation-labelled shell request did not prompt and retained the same outer
grants. This does not establish native approval-rule equivalence. Unsupported
existing requirements, rules, hooks, and policy sources remain production
blockers; a private HOME must not hide them.

### Ordinary nested sandbox limitation

An earlier diagnostic failed while loading instructions with a denied
`NETLINK_ROUTE` socket. The 0.4 probe made the exact binary visible through the
controlled executor and demonstrated provider contact and a text-only turn.
When the fixture requested `exec_command`, the workspace-write tool path still
failed with:

```text
bwrap: No permissions to create a new namespace
```

The outer executor intentionally prevents that nested namespace/mount operation.
The App Server fixture demonstrates a documented external-sandbox integration;
it does not prove nested bubblewrap now works. Current official
[permissions documentation](https://learn.chatgpt.com/docs/permissions) also
rejects the legacy Landlock backend for filesystem-restricted policies, so that
setting is not a supported fallback. No dangerous bypass flag or weaker outer
filesystem/network scope is introduced.

## The 13 acceptance checks

The fixed fixture returns one `exec_command` call in its first response. The
command writes inside an owned directory and probes forbidden access. The second
response completes the turn after the tool result returns. Every assertion must
be true; process exit 0 alone is insufficient.

| Assertion in `native_acceptance.ASSERTIONS` | Required evidence |
| --- | --- |
| `native_process_succeeded` | Runner exit 0 without timeout or cancellation. |
| `complete_native_turn` | Normalized output passes independent native success validation. |
| `strict_boundary_attested` | The actual controlled executor attests the requested strict boundary. |
| `process_tree_terminated` | The outside supervisor proves termination of the complete worker tree. |
| `exact_model_and_quality` | Exact model; broker-enforced wire quality constraints; runner-verified supplied native quality settings. |
| `native_delegation_absent` | The accepted outgoing manifest exposes none of the prohibited delegation names. |
| `two_model_requests` | Exactly two synthetic requests, with no fixture errors. |
| `tool_result_returned` | The native tool result appears in the second provider request. |
| `scoped_write_succeeded` | The owned file contains the expected data. |
| `sibling_write_denied` | The protected sibling is unchanged and the tool reports denial. |
| `git_write_denied` | The repository metadata write is denied and its target stays absent. |
| `external_network_denied` | Direct connection to the external test address fails. |
| `controller_file_hidden` | The worker cannot read the controller-only canary. |

The accepted fixture uses a flat function/custom tool manifest.
`agents.enabled=false` and the observed manifest jointly establish suppression
for this path, not every model's tool schema or arbitrary shell-created
processes. Unsupported tool declarations still fail at the broker. Another
model/tool surface requires its own compatible acceptance result.

The controller-owned registry binds the snapshot, provider configuration,
deployment, binary/runtime identity, project policy, and report. Identity and
policy are checked before and after acceptance and again before launch. See
[native_registry.py](../src/xiaolvji/native_registry.py) and
[native_policy.py](../src/xiaolvji/native_policy.py). A fixture report alone cannot
bypass these checks.

## Six installed-Codex opt-in cases

The actual tests use a fresh private HOME, the real strict executor, a fixed
local provider fixture, and synthetic credentials. They do not read ambient
auth, log in, or contact an external model. A complete bundled metadata entry is
copied under a synthetic model slug for the fixture only; a user's real model
is never substituted.

1. The production runner passes all 13 native acceptance checks.
2. Effort, summary, verbosity, service tier, context window, and automatic
   compaction threshold match the supplied values. Wire constraints also verify
   the documented `fast` to `priority` tier mapping.
3. Real `exec_command` and `write_stdin` complete a three-request provider loop
   and an owned file write.
4. Cancellation while native Codex waits for a provider response closes the
   connection, terminates the complete tree, and produces no success terminal.
5. Preserving this machine's real `/etc/codex` read-only produces
   `NATIVE_MANAGED_REQUIREMENTS_UNSUPPORTED` with zero provider requests.
6. An unknown model without catalog metadata produces
   `NATIVE_MODEL_UNSUPPORTED` with zero provider or native tool requests.

Run the suite against an explicitly selected installed executable:

```sh
XIAOLVJI_CODEX_NATIVE_SMOKE=/opt/codex/bin/codex \
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p test_codex_app_server.py -v
```

Without `XIAOLVJI_CODEX_NATIVE_SMOKE`, the six installed-host cases are explicitly
skipped; hermetic protocol and process tests still run. The managed-policy case
also skips on machines without the real requirements fixture. A skip is not a
passed native integration test. The direct synthetic tests intentionally do not
create a production registry record for this managed machine.

Smaller configuration-loading checks remain separate:

```sh
XIAOLVJI_CODEX_CLI_SMOKE=/opt/codex/bin/codex PYTHONPATH=src \
  python3 -m unittest discover -s tests -p test_adapters.py -v

XIAOLVJI_CODEX_CLI_SMOKE=/opt/codex/bin/codex PYTHONPATH=src \
  python3 -m unittest discover -s tests -p test_host_config.py -v
```

They load configuration and exit; they do not replace the native turn, tool,
cancellation, or managed-policy cases.

## Catalog, configuration, and transport

The owner snapshot remains the authority for provider/model/quality selection.
Optional `codex_model_catalog` carries complete native metadata with one exact
selected slug and declared capabilities. It is sealed at
`.codex/model-catalog.json` and selected by `model_catalog_json`. The native
loader and `model/list` must accept it. No production metadata is synthesized,
no model alias is guessed, and generic fallback is not accepted. The official
[gateway guidance](https://learn.chatgpt.com/docs/enterprise/roll-out-a-gateway)
describes the role of explicit metadata for custom model identifiers.

The renderer pins `web_search="disabled"` because provider-hosted search is not
authorized by this transport. Native Responses `client_metadata` is allowed only
as an opaque string map: at most 32 entries, 1–128 UTF-8 bytes per key, at most
4096 UTF-8 bytes per value, and at most 32768 canonical JSON bytes in total.
Nested/non-string values fail. Metadata cannot change routing, model, quality,
or tool authority.

The worker reaches its private-loopback bridge. The outside broker retains the
explicit upstream endpoint and credential; the attempt capability is not an
upstream key. Exact mappings, sealed files, and environment keys are documented
in [HOST-CONFIG-CONTRACT.en.md](HOST-CONFIG-CONTRACT.en.md).

## Completion and process accounting

App Server is resident. After verified native completion and no pending client
RPC, the runner closes stdin, waits briefly, then sends its owned child TERM and
KILL if necessary. It waits for service exit and emits `host.service_stopped`
with the actual exit code and requested signal before normalized
`turn.completed`. Unexpected service failure, cancellation, or timeout is not
converted into success.

The service-stop record explicitly contains `process_tree_terminated: false`.
Only the outside supervisor proves termination for all descendants. Native
terminal events and runner exit 0 never release a slot, accept a candidate, or
replace quality checks and review.

## Hook and MCP boundary

The official [Hooks documentation](https://learn.chatgpt.com/docs/hooks)
requires review and trust for applicable non-managed hooks. This integration
does not change trust state. Installed-plugin Hook registration, native event
coverage, worker identity, and denial behavior remain separate acceptance gates.

A zero-provider-request diagnostic connected an isolated stdio MCP server.
App Server reported server `parallel-development`, raw tool
`parallel_dev_status`, and no plugin identity. This proves direct inventory
loading only. A model turn with that server declared deferred client
`tool_search`; the broker rejected this unsupported declaration. Its allowlist
was not broadened to make the diagnostic pass.

Current public Codex source sanitizes the MCP namespace prefix to ASCII letters,
digits, and underscores; its MCP Hook handler joins the canonical namespace and
tool name. This supports the source-level `mcp__parallel_development__` spelling.
It is not an observed installed-plugin Hook event for this binary. The moving
source references below do not certify the complete local plugin path:

- [Current MCP namespace sanitizer](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/mcp/mod.rs).
- [Current MCP tool and Hook handler](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/mcp.rs).

Native plugin/MCP/Hook acceptance remains separate from the verified shell-tool
fixture. Resource accounting continues to depend on controller-owned identity,
outer scope enforcement, and complete process-tree termination.
