# Strict Linux sandbox and scoped provider transport contract

Status: runtime 0.4 retains the `controlled_executor` Linux offline default and
implements an explicit provider channel verified with synthetic local fixtures.
The implementation is `src/xiaolvji/sandbox.py`; the trusted execution supervisor
owns admission, launch attestation, cancellation and final process-tree evidence.
Actual Codex tool execution through this transport passed a synthetic native
protocol fixture. Production execution still requires a fresh deployment-specific
acceptance record; this managed development host remains blocked. Complete ZCode
CLI and real-provider acceptance are not established. See the
[native host contract](NATIVE-HOST-CONTRACT.en.md); an offline or synthetic pass
alone does not authorize another deployment.

## 1. Supported guarantee

An admitted command and its descendants receive a private filesystem view, new
user, PID, mount, IPC, UTS and network namespaces, no Linux capabilities, and an
inherited seccomp filter. The command cannot access an unmounted controller
database, role credential, candidate repository, sibling workspace, host home,
host service socket or shared Git ref through its normal filesystem view.
Declared source write scopes are enforced by read-only and writable bind mounts.
Kernel enforcement remains active when the command ignores the skill or hooks.

By default this is an offline boundary. The network namespace has no configured interfaces,
including no configured loopback interface. The filter also denies creation of
non-UNIX sockets. An explicit controller-issued provider channel enables only
private IPv4 loopback and one inherited unnamed UNIX socketpair for that attempt.
There is no host-network switch, automatic profile downgrade, implicit provider
proxy, copied login session or upstream credential forwarding. Verification and
integration checks continue to use the offline boundary.

The trust model includes the controller and its provider broker, the trusted supervisor/bootstrap,
the system `unshare` and `bwrap` binaries, the OS kernel, and explicitly mounted
runtime/toolchain data. A malicious command inside the boundary is in scope.
A malicious administrator or unrelated hostile process already running outside
the boundary with the controller's account is outside this boundary's protection.
The controller must exclusively own the private clone while preparing and running
an attempt. It must not let another host process replace mount sources during
launch. OS/kernel vulnerabilities and physical side channels are not claimed to
be solved by this backend.

Read-only grants are readable. The backend does not promise confidentiality for
files deliberately included in the toolchain or in an explicit read-only grant.
Administrators must keep controller credentials and other private data out of
those locations. The default policy does not mount the host root, the whole host
home, the whole `/etc`, the whole `/opt`, `/run`, or a host `/proc` instance.

## 2. Prerequisites and fail-closed behavior

The backend requires:

- Linux with working unprivileged user, network, PID, mount, IPC and UTS namespaces;
- a native little-endian x86_64 or aarch64 ABI;
- system `bwrap` **0.12.0 or later**, without setuid/setgid bits;
- system util-linux `unshare` with `--user --map-current-user --net` support;
- kernel seccomp filtering, sealed memfd support and the supervisor's separate
  pidfd/subreaper requirements;
- a usable Python runtime available through a narrow read-only runtime mount.

The visible owner of system files may be an overflow UID when already running in
a user namespace. The executable trust check therefore examines non-setuid mode
and whether the non-root caller can write the executable or its path ancestors
below `/`, instead of insisting that the visible owner is numerically UID zero.
The host filesystem root itself remains part of the trusted launcher environment.

The minimum bubblewrap version is deliberate: upstream identifies versions below
0.12.0 as affected by sandbox-setup symlink traversal, CVE-2026-87766. A distribution
backport is not silently assumed to be fixed. Unsupported versions fail the
capability check until an explicit, reviewed capability policy supports them. [1]

`LinuxSandbox.probe()` performs a bounded real execution. It verifies different
namespace identifiers, private workspace writing, an inaccessible outside marker,
a read-only sandbox root, a private HOME, `NoNewPrivs=1`, seccomp mode 2, zero
effective/permitted/bounding capabilities, and denied ptrace. A failed probe
returns `supported=false` and a reason. It does not run a cooperative fallback.
Per-attempt supervisor attestation is still required after a successful probe.
The offline probe does not establish provider transport or native-host readiness.

### Empty network namespace construction

The launcher is:

```text
system unshare --user --map-current-user --net
  system bwrap --unshare-user --unshare-ipc --unshare-pid --unshare-uts
    --disable-userns --cap-drop ALL --die-with-parent --new-session ...
```

`unshare` creates an empty network namespace without configuring loopback.
Bubblewrap creates the remaining boundary and executes the trusted bootstrap.
This avoids requiring a `NETLINK_ROUTE` socket merely to configure loopback on
hosts where an enclosing policy denies that operation. It does not retain the
host network namespace. The bootstrap must attest the actual network namespace
identifier, together with the other namespace identifiers, before command release.

### Explicit private loopback construction

With a provider channel, `unshare` additionally uses `--keep-caps` to run a fixed
isolated Python helper before bubblewrap. The helper verifies that it entered a
different network namespace containing only `lo` and no external IPv4 routes,
enables loopback using `SIOCSIFFLAGS`, then clears its effective, permitted,
inheritable and ambient capabilities before executing bubblewrap. Bubblewrap
independently applies `--cap-drop ALL` to the final boundary. No task code runs
during this setup, and no network namespace or route is shared with the host.
The final bootstrap must attest private loopback, absent external routes, and
zero effective, permitted, inheritable, ambient and bounding capabilities.

The seccomp variant permits `AF_INET` only for this explicit profile. A real
fixture verified that a private listener can bind the same loopback address and
port already occupied by a controller listener, without reaching that listener.
This does not add general outbound networking to the sandbox.

## 3. Filesystem grants

Bubblewrap begins with a separate empty filesystem root. The root is remounted
read-only after construction. The runtime mounts only the required categories:

| Grant | Access | Purpose |
| --- | --- | --- |
| Curated `/usr` executable/library subtrees and selected runtime data | Read-only | Shell, Git, Python and ordinary local build tools |
| The current Python runtime prefix when outside standard `/usr` locations | Read-only | Run the trusted bootstrap and Python-based checks |
| Selected loader/timezone files | Read-only | Dynamic loader and local runtime operation |
| Synthetic `/etc/passwd` and `/etc/group` | Read-only | A sandbox user identity, without copying host account files |
| Private clone at its original absolute path | Read-only by default | Stable cwd and source paths |
| Declared existing write-scope directories or files | Writable | Only the admitted implementation's source changes |
| Top-level clone `.git`, if present | Read-only | Prevent worker changes to clone administration |
| `/home/xiaolvji` | Private writable tmpfs | Disposable application configuration/cache |
| Explicit private HOME configuration files | Read-only sealed data | Immutable controller-rendered model/quality configuration; no host account store |
| `/tmp` | Private writable tmpfs | Disposable compiler/test scratch and runtime files |
| New `/proc` | Read-only | Only processes visible in the new PID namespace |
| New `/dev` | Private device view | Standard devices and local shared-memory plumbing |
| Explicit extra read-only inputs | Read-only | Controller-approved narrow data or trusted source files |
| Explicit extra writable directories | Writable | Optional narrow outputs, never controller state |

The implementation contains the exact default toolchain list. It does not mount
a broad parent directory to make an unknown executable work. A command requiring
another toolchain must receive a separately reviewed narrow input grant or fail.
The synthetic home is empty unless the controller supplies explicit `home_files`.
No host `HOME`, `CODEX_HOME`, XDG state,
SSH agent, D-Bus socket or model login state is copied.

Each private HOME file has a validated relative path and UTF-8 content. The closed
schema permits at most 16 files and 2 MiB of aggregate content, rejects overlapping
paths, traversal and reserved components, and mounts each file read-only with mode
`0600`. Each ancestor directory gets its own private tmpfs mount, preventing a
worker from renaming that ancestor to substitute a different configured path.
Sibling cache files remain writable. Policy evidence records paths, sizes, modes
and content hashes; it does not contain the configuration contents or capability.

The original absolute workspace path is preserved inside the sandbox. Its parent
directories are recreated in the private filesystem view; this does not mount
their host contents. A task can create data elsewhere in its private tmpfs areas,
but this does not grant writes to another host source tree.

### Scope semantics

`write_scope` uses the existing `{path, kind}` task schema:

- `[]` makes the entire checkout read-only. Verifier checks use this form.
- Existing directory entries become writable bind mounts over the read-only
  checkout. They permit normal create, delete, rename and atomic replacement
  within that declared directory.
- Existing exact-file entries become writable file bind mounts. Direct writing
  works. Deleting/replacing that mount point, including common atomic replacement
  strategies, does not work. The parent directory is never made writable to work
  around this limitation.
- A missing directory must be created by the trusted controller before launch.
  A missing exact file fails closed. Preparation does not silently expand a scope
  or precreate a source file on the command's behalf.
- `None` is an explicit whole-private-workspace write grant for the low-level
  API. The top-level `.git` is still overlaid read-only. Managed implementation
  tasks must pass their declared scope array; they must not use `None`.

Git administration cannot be named as a write scope. Newly created nested Git
repositories inside writable source directories remain subject to the existing
post-run workspace audit. The mount boundary protects the top-level clone's
administration; it does not replace source-format or candidate-integrity checks.

An existing clone is scanned before any process receives its mount. The scan is
bounded at 200,000 entries. It rejects multiply linked regular files, special
files, preexisting sockets/FIFOs/devices, escaping source symlinks, symlinked
scope roots, and nested filesystem or bind mounts. Private clones must be copied
without hardlinks to shared objects. Preexisting cross-tree symlink dependency
stores are unsupported unless copied into a safe source layout.

The preflight scan alone is not the runtime boundary. A running command can create
a symlink inside an allowed directory, but that symlink resolves in the restricted
namespace. A link to an unmounted controller path remains inaccessible; a link to
a mounted read-only sibling remains read-only. A newly attempted hardlink between
different bind mounts cannot grant write access to the read-only source inode.
These cases have executable regression tests.

## 4. Syscalls, capabilities and process isolation

The launch uses `--cap-drop ALL`, `--disable-userns`, `--new-session` and
`--die-with-parent`. Bubblewrap sets `no_new_privs`. The seccomp program is placed
in a sealed memfd and loaded before untrusted execution. Synthetic account files
also use sealed memfds. These setup descriptors are consumed by bubblewrap and
are not available to the final command.

The filter first validates the syscall architecture. Non-native ABIs are killed;
x32 calls are separately rejected on x86_64. It then denies ptrace, cross-process
memory access, pidfd descriptor theft, namespace entry/creation, mounts, handle-
based filesystem access, kernel keyring calls, BPF/perf interfaces, module loading,
reboot/kexec, swap administration, device creation, userfaultfd, io_uring and the
new mount API. Namespace flags in ordinary `clone` are denied. `clone3` returns
`ENOSYS` so standard libraries can fall back to ordinary clone for threads.
Additional tighter seccomp restrictions remain possible; the existing filter
cannot be removed by a child. Seccomp is one component of this boundary, not a
standalone filesystem sandbox. [2]

Normal threads, fork, exec and local UNIX socket pairs continue to work. Socket
families other than `AF_UNIX` are denied by the offline filter. The explicit
provider variant additionally permits `AF_INET` inside its private namespace;
INET6, NETLINK, PACKET and VSOCK remain denied, and `socketpair` remains UNIX-only.
Host pathname sockets are absent from the default view, preexisting source sockets
are rejected, and the new network namespace also separates abstract UNIX sockets.
`TIOCSTI` and `TIOCLINUX` ioctls are denied in addition to the new terminal session.

The new PID namespace and new procfs prevent numeric process operations from
targeting an ancestor namespace's controller processes. A command cannot recover
host filesystem authority through a host process's `/proc/<pid>/root` or file
descriptors because those host processes are not visible in its procfs. [3]

Bubblewrap keeps a PID-1 reaper inside the sandbox. Linux kills the remaining
namespace processes when that namespace's init terminates. The trusted outer
supervisor must still record the namespace init identity, validate actual launch
evidence, and establish final tree termination before returning a concurrency
slot. A CLI exit, native completion hook or observed main-process exit alone is
not that evidence. If the trusted journal is incomplete, existing uncertain-
termination semantics continue to charge the slot. [3][4]

## 5. Environment policy

Only a small enumerated set of locale/terminal/CI values is preserved. The backend
constructs its own PATH, HOME, temporary directories, XDG directories and Git
global-configuration settings. It forces `PYTHONDONTWRITEBYTECODE=1`.

`PARALLEL_DEV_ROLE` is the fixed informational value `worker`; a syntactically
validated `PARALLEL_DEV_ATTEMPT_ID` may be preserved for log correlation. Neither
environment marker conveys reviewer, integrator or controller authority. Provider
credentials, arbitrary engine variables, import/loader injection settings,
controller tokens and host configuration paths are not copied.

This environment policy does not reproduce a user's Codex/ZCode login or discover
ambient model settings. The explicit host renderer accepts an owner-supplied,
immutable model and quality snapshot and rejects unsupported combinations. The
trusted bridge adds only the renderer's closed set of private configuration paths
and attempt-scoped capability values to the final host command. Upstream
credentials remain in the outside controller. Model identity and supported quality
constraints are independently checked by the broker on every request; there is no
silent model substitution or quality downgrade. The Codex adapter uses the
explicitly selected external-sandbox profile only after fresh deployment
acceptance. It cannot preserve every native approval semantic, so unsupported
existing policies block; the current managed host remains ineligible. It never
auto-approves native requests or discards applicable rules. See
[NATIVE-HOST-CONTRACT.en.md](NATIVE-HOST-CONTRACT.en.md) for policy scope and
[HOST-CONFIG-CONTRACT.en.md](HOST-CONFIG-CONTRACT.en.md) for renderer limits.

## 6. Python API and attestation contract

```python
with LinuxSandbox(state_dir).prepare(
    bootstrap_argv,
    cwd,
    safe_environment,
    workspace=private_clone,
    write_scope=[{"path": "module", "kind": "directory"}],
    readonly_paths=(),
    writable_paths=(),
    provider_transport=None,
    home_files=(),
) as launch:
    # The trusted supervisor supplies the pipe handshake and owns this process.
    process = subprocess.Popen(
        launch.argv,
        env=launch.env,
        pass_fds=launch.pass_fds,
        close_fds=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
```

`SandboxLaunch` has `argv`, `env`, `pass_fds`, `evidence` and optional `provider_fd`; leaving the context
closes launcher-owned descriptors. It does not start a command or write a launch
journal. Extra grants and write scopes are trusted-controller policy inputs, not
worker-supplied capability requests.

The explicit transport input is `provider_transport={"fd": descriptor}`. Preparation
duplicates the descriptor before inspecting it, pins that duplicate, and accepts
only a controller-owned, connected, unnamed `AF_UNIX`/`SOCK_STREAM` endpoint with
a peer belonging to the controller user. It accepts no socket pathname, host
listener, shared socket directory or arbitrary descriptor set. The broker, not
socket metadata alone, authenticates the grant and its attempt binding.

`evidence` contains:

```json
{
  "backend": "linux-bubblewrap-offline-v1",
  "policy_digest": "sha256:<canonical-policy-digest>",
  "policy": {
    "version": 1,
    "workspace": "/absolute/private/clone",
    "write_scope": [{"path": "module", "kind": "directory"}],
    "readonly_paths": [],
    "writable_paths": [],
    "network": "none",
    "readonly_mounts": ["<exact-destinations>"],
    "writable_mounts": ["<exact-destinations>"],
    "namespaces": ["user", "pid", "mnt", "net", "ipc", "uts"],
    "seccomp": "xlj-offline-v1",
    "architecture": "x86_64",
    "home": "/home/xiaolvji"
  },
  "tool_versions": {"bwrap": "<observed>", "unshare": "<observed>"}
}
```

The backend identifier remains `linux-bubblewrap-offline-v1` for compatibility;
the full canonical policy determines the actual boundary. Explicit provider
launches set `network="brokered-provider-v1"` and `seccomp="xlj-brokered-v1"`, and
add the pinned channel's device/inode/ownership/peer metadata. The transient source
descriptor number is excluded from the policy digest. `guest_fd=3` identifies the
final bridge handoff, not the source descriptor used during attestation. Optional
`home_files` metadata and their exact mount destinations also enter the policy.
Without either extension, the previous offline policy and digest shape are unchanged.

The policy digest describes the requested policy. It is not a launch attestation.
The outer supervisor must verify a trusted pre-command bootstrap's actual
namespace identifiers, kernel status, observed mount flags and hidden controller
state, bind that observation to owned process identities, durably record it, and
only then release the untrusted command. It must close bootstrap control
descriptors and replace stdin with the intended command input before exec.

For provider launches, the bootstrap additionally reports the pinned channel's
kernel identity, UNIX stream type, unnamed endpoints and peer credentials, plus
the loopback-only namespace and absent external IPv4 routes. The supervisor
compares these observations with the admitted policy before release. Only after
release does the trusted bootstrap remap the channel to descriptor 3 for the
fixed HTTP bridge. The host command receives loopback HTTP and a synthetic grant
capability; `close_fds=True` prevents it from inheriting the broker descriptor.
Controller copies of the worker endpoint close after handoff so they cannot mask
a worker-side disconnect.

Quality reports and immutable check manifests belong to the trusted outer check
runner. They are not writable mounts for tested commands. Each actual check uses
the strict executor with a read-only checkout and private temporary storage.
Passing tests still undergo the existing immutable-artifact and external-review
gates; a filesystem sandbox does not decide whether code is correct.

## 7. Provider admission, budgets and revocation

The outside broker binds each channel to one admitted attempt ID, generation and
launch key. It checks authoritative admission on requests and rejects stale,
cancelled or revoked grants. Explicit configuration pins the provider API, HTTPS
endpoint, model and supported quality constraints. The broker synthesizes upstream
authentication; the worker cannot select upstream URLs or authentication headers.
Host account stores, OAuth sessions and ambient provider environment variables are
not imported. Explicit operator-named credential files or programmatic controller
credentials stay outside the sandbox.

Configured limits cover accepted requests per attempt, bytes per request and
response, request and grant deadlines, and local requests in flight globally and
per provider. Each channel carries one sequential request at a time. Admission
and budget charging precede upstream I/O; failed upstream requests remain charged.
Request slots and development slots are separate accounting domains. These limits
do not claim token billing caps, remote compute quotas or a kernel process budget.

Attempt cancellation, lost admission, expiry, worker disconnect and controller
shutdown revoke local transport and close its upstream connections. Durable
history prevents reuse of an attempt generation. Local request capacity becomes
available through owned request cleanup. Revocation does not prove that the worker
process tree exited, so the executor must still confirm tree termination before
the ledger releases a development slot. A local disconnect also does not prove
that the upstream provider stopped processing; that remote state remains unknown.

## 8. Limits and validation status

This backend does **not** claim cgroup CPU, RAM, process-count, I/O or aggregate
resource budgets. Private tmpfs storage is not a complete denial-of-service
defense. Wall-clock timeout, bounded logs and process-tree cancellation are owned
by the execution layer. Ledger reservations count admitted development attempts;
they are not a kernel limit on every compiler thread or subprocess inside one
attempt. There is no Windows/macOS implementation of this boundary.

On 2026-11-09, real x86_64 checks ran with Linux 6.18.57, bubblewrap 0.12.0 and
util-linux 2.41.5. The sandbox module's 28 tests passed without skips, covering
positive workspace/home/tmp use, read-only and directory/file scopes, future-scope
rejection, controller/proc escape paths, runtime symlink/hardlink attempts,
preexisting aliases and IPC endpoints, outside-process signaling, syscall and
socket restrictions, environment isolation, detached-descendant cleanup, pinned
provider descriptors, private loopback and immutable private HOME configuration.
Nine additional provider execution tests passed without skips using a synthetic
local upstream and Python HTTP client. They exercised real namespace attestation,
live SSE/JSON, credential separation, rejection before upstream use, disconnect,
cancellation and timeout, plus a real Git/ledger pipeline with three independent
tasks and two slots. The pipeline refilled while an unrelated SSE worker remained
active, kept all verification and integration checks offline, and retained the
independent-review gate. These are transport/lifecycle fixtures, not successful
native Codex or ZCode model runs.
The aarch64 filter table is implemented, but this environment did not provide a
real aarch64 execution test. A successful host probe and per-launch attestation
are required there as on x86_64. End-to-end executor/pipeline validation is recorded
separately in the release status.

## Primary references

1. Bubblewrap upstream advisory, CVE-2026-87766, affected `<0.12.0`, patched `0.12.0`:
   <https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx>
2. Linux kernel seccomp-filter documentation, architecture checks, inheritance and
   the limits of syscall filtering:
   <https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html>
3. Linux man-pages project, PID namespace visibility, procfs and namespace-init
   termination semantics:
   <https://man7.org/linux/man-pages/man7/pid_namespaces.7.html>
4. Bubblewrap upstream option reference, process lifecycle and descriptor options:
   <https://github.com/containers/bubblewrap/blob/main/bwrap.xml>
5. Bubblewrap v0.12.0 upstream README, policy responsibility, empty filesystem
   construction, namespace behavior, no-new-privileges and mounted-service risks:
   <https://raw.githubusercontent.com/containers/bubblewrap/v0.12.0/README.md>

References checked 2026-11-05. The installed binaries and real kernel probes,
not documentation alone, determine whether this backend can run on a host.
