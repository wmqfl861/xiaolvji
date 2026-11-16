# Scoped provider transport contract — 0.5.0

Updated 2026-11-09. This contract describes implemented code and synthetic integration evidence. Actual Codex synthetic protocol evidence is separate from production policy eligibility and real external provider compatibility. See [HOST-CONFIG-CONTRACT.en.md](HOST-CONFIG-CONTRACT.en.md), [STRICT-SANDBOX-CONTRACT.en.md](STRICT-SANDBOX-CONTRACT.en.md), and the selected release's `VALIDATION.json`.

## Authority and topology

The trusted controller owns upstream credentials, a single-owner durable broker journal, registered HTTPS profiles, and the ledger admission callback. Each admitted implementation attempt receives one connected, unnamed Unix stream socketpair endpoint and a fresh random capability. There is no public Unix listener, filesystem socket path, worker-controlled destination, or host-network fallback.

The executor passes only that endpoint to its trusted supervisor. Preflight and actual startup bind its kernel identity to the requested sandbox policy. The supervisor's fixed bootstrap verifies isolated namespaces, capability sets, mount flags, a loopback-only network without external IPv4 routes, and the inherited channel identity. It persists the attestation before authorizing the private bridge and task command. An unstable descriptor number is not a policy identity.

The bridge runs inside the worker's private network namespace on `127.0.0.1:38119`. Native-client fixtures use ordinary HTTP there; the bridge sends framed JSON over the inherited endpoint. The model CLI itself inherits only standard descriptors. Only explicitly rendered configuration paths and an attempt-scoped capability enter its environment; actual provider credentials remain outside the boundary. The broker independently validates requests, so the bridge is not an authorization authority.

Default controlled commands and all quality checks remain offline. No quality check receives this endpoint, host configuration, or model capability. A controller or hostile administrator already outside the sandbox is trusted by this design; the channel does not establish a boundary against them.

## Provider profiles

`normalize_provider_profile(value) -> ProviderProfile` validates a closed schema:

| Field | Meaning |
| --- | --- |
| `profile_id` | Unique registered identifier |
| `api` | `openai-responses`, `openai-chat-completions`, or `anthropic-messages` |
| `upstream_url` | Exact HTTPS endpoint, including its path; no credentials, query, or fragment |
| `model` | Exact model identifier |
| `request_constraints` | Exact required native quality values, excluding the separate model field |
| `max_requests` | Per-attempt consumed-request budget, default 32, range 1–1024 |
| `max_request_bytes` | Canonical request JSON budget, default 2 MiB, maximum 16 MiB |
| `max_response_bytes` | Upstream response-body budget, default 8 MiB, maximum 64 MiB |
| `request_timeout` | Absolute request deadline, default 120 seconds, maximum 3600 |
| `max_concurrency` | Local per-provider in-flight limit, default 4, maximum 64 |

The broker has a separate global local-request limit, range 1–64. One request can be active on an attempt channel at a time. Policy decisions use frozen canonical profile data, not subsequently mutated caller dictionaries. HTTPS certificate and hostname verification are enabled; no redirects, proxies selected by worker headers, or plaintext production connector exist.

The provider configuration layer binds explicit project/engine snapshots to these profiles. API, model and quality values must match exactly, including numeric types. The broker requires those values in the actual request and refuses a mismatch; it never silently substitutes a different model or effort.

Native request fields are allowlisted. Background jobs, remote provider tools, remote input references, multiple generated choices, and explicit `store:true` are rejected. Inline data and client-executed tool declarations have a deliberately narrow supported shape. Responses `client_metadata` is an opaque string map: at most 32 entries, keys of 1–128 UTF-8 bytes, values of at most 4096 bytes, and at most 32768 total bytes. It cannot select routing, credentials, model, quality or tools. Nested/non-string values and invalid UTF-8 are rejected before request-budget consumption. This is not complete support for every optional feature of the three public APIs, nor a guarantee about a provider's own persistence defaults. Unsupported namespace/tool-search declarations remain rejected; the runtime does not change model to obtain a supported tool manifest.

## Framing and bridge limits

Frames are a four-byte unsigned big-endian length followed by bounded strict UTF-8 JSON. Duplicate keys and non-finite numbers are rejected. One request has exactly:

```json
{
  "v": 1,
  "request_id": "unique-within-this-grant",
  "capability": "per-attempt-random-value",
  "method": "POST",
  "path": "/v1/responses",
  "body": {"model": "exact-model", "input": "task data", "reasoning": {"effort": "selected-effort"}}
}
```

The exact local route is selected from the registered API. The worker supplies no upstream URL, authentication header, organization header, file path, or TLS setting. Requests cannot replay their IDs. Replies contain one `response` frame, base64 `data` frames, and terminal `end` or `error`. Stream IDs must match. Pipelining is not supported.

The loopback HTTP bridge bounds request lines, headers before parsing, JSON bodies, absolute read/stream deadlines, and pending connections. It rejects ambiguous content lengths, transfer-encoded/compressed requests, unsupported content types, wrong routes and missing capabilities. It forwards no worker headers beyond its closed protocol. An HTTP client may close immediately after reading the terminal SSE event, while the broker's matching `end` frame is still arriving. The bridge preserves partial IPC framing and permits at most 150 ms to drain that same response. Only a valid matching `end` permits channel reuse. Missing/mismatched/error terminal frames, excess HTTP bytes, cancellation or deadline close the channel. The drain does not admit a new request, replay a request, refund budget or buffer the entire response. Streaming remains incremental.

Upstream handling accepts only successful JSON/SSE responses with identity content encoding and conventional content-length, EOF or chunked framing. It rejects ambiguous framing, redirects, interim HTTP responses and chunk extensions. Response headers are not copied wholesale. Non-success response diagnostics are withheld; exact credential strings are redacted across response chunk boundaries. This limited exact-string redaction does not claim to recognize arbitrary encodings or transformations by a malicious provider.

## Durable lifecycle and recovery

```python
broker = ProviderBroker(state_dir, profiles, credentials, admission_check,
                        global_concurrency=4)
await broker.start()
grant = await broker.grant(attempt, provider_id, ttl_seconds=3600)
# Grant: binding, capability, policy_digest, worker_socket.
grant.close_worker_endpoint()  # after the supervisor inherits its copy
await broker.revoke(attempt_id, generation)
status = broker.status()
await broker.close()
```

The admission callback receives `{attempt_id, generation, launch_key}` and a phase. Grant creation requires the current claimed `STARTING` attempt; requests require `RUNNING` with a durable process identity. `ProviderRuntime` refreshes the authoritative ledger record before selecting project, engine and provider. Disabled projects, cancellation, stale generations, changed binding, `LOST`, and released attempts cannot authorize requests. A watchdog checks admission during an active stream as well as before a new request.

The SQLite journal uses single-owner locking and full synchronization. It commits consumed request budget before outbound connection. Failed or uncertain requests retain their consumed budget. A grant generation and request ID cannot be reused. Request counters and owned cleanup survive repeated revocation or cancellation of the caller performing cleanup. Completed in-memory leases are retired so rolling work is not limited to the live-lease ceiling over the entire controller lifetime; durable replay history remains.

Revocation, expiry, admission loss, unfinished downstream disconnect (after the bounded terminal drain), and controller shutdown remove local authority and abort local upstream transport. An actual killed-controller regression observes channel EOF and upstream disconnect. On restart, old grants are revoked and unfinished requests become `UNCERTAIN`; capability material is not reconstructed, and requests are not retried.

These facts do not prove that a remote provider stopped computation or billing. Local HTTP-request capacity and managed worker capacity are separate accounting domains. A revoked grant does not prove worker process-tree exit. Only the existing supervisor/namespace/subreaper evidence can release a started managed worker slot; missing evidence remains charged.

## Configuration and runtime APIs

`provider_config.py` exports `normalize_provider_configuration`, `load_provider_configuration` and `read_explicit_credentials`. The version-one root fields are `schema_version`, `providers`, `bindings`, `credential_files` and `global_request_limit`. Each binding has `project_id` and a validated `host` snapshot. Returned configuration values are detached copies.

Validation and doctor never open credential references. Actual startup reads only explicit private regular files with current-user ownership, no aliases or multiple links, no group/other access, and bounded ASCII content. Enrolled source/workspace locations and every implicit readable runtime mount are rejected, using the same `sandbox.readable_host_sources()` inventory that constructs the actual mounts. This includes resolved aliases, file mounts and nonstandard Python prefixes; a read-only mode0600 file can otherwise remain readable by the mapped worker UID. No environment, existing account, connector credential or OAuth store is discovered automatically. Errors omit file contents and raw paths.

`ProviderRuntime(state_dir, ledger, configuration, *, broker_factory=ProviderBroker, credentials=None)` supports `start`, `bind`, `handed_off`, `release`, and `close`. The optional programmatic factory/credential arguments are trusted embedding and fixture seams, never task JSON or environment-selected escape hatches. Pipeline jobs persist immutable provider/config/quality bindings but not runtime capability values or transient FD numbers. Release and shutdown revoke grants even when execution preparation fails.

The CLI adds `provider validate --file PATH`, optional `doctor --provider-config PATH`, and `run --provider-config PATH`. Native strict model execution requires a fresh controller-owned deployment acceptance record before granting access or reading credentials. `host verify` runs the fixed credential-free native fixture only after the actual deployment policy passes; `doctor` inspects the exact record and current identity. There is no CLI bypass for missing native-host acceptance. A deterministic `demo --profile controlled_executor --provider-transport` exercises the real transport and full quality pipeline with a local synthetic service.

## Acceptance scope and remaining work

Real tests cover socketpairs, namespace isolation, immutable host files, HTTP and live SSE, wrong-route/model/quality denial, credentials absent from the worker, budgets, concurrent revoke, request backpressure, controller death, restart accounting, rolling refill and cancellation. Quality checks remain offline and final delivery awaits independent review.

Installed Codex `0.167.0-alpha.4` completed real native tool turns through the production App Server runner and this channel using a fixed local provider fixture. Six native tests include exact quality, multi-tool continuation, cancellation, preserved managed-policy denial and unknown-model denial. The installed host remains ineligible for production standalone mode because its management policy cannot yet be transferred completely. ZCode's complete pinned source was reconstructed and its pure option mappings executed, while its full CLI build remains blocked by unavailable npm access. Installed-plugin Hook/MCP integration, real provider compatibility, managed-policy preservation, non-Linux backends and long-duration load remain independent acceptance work. See [NATIVE-HOST-CONTRACT.en.md](NATIVE-HOST-CONTRACT.en.md) and both host compatibility documents.
