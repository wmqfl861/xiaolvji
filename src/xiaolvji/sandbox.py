"""Fail-closed Linux isolation, offline unless a provider channel is explicit.

The trusted supervisor owns admission, command release and reaping.  This module
only constructs the boundary; requested flags are never execution attestation.
See docs/STRICT-SANDBOX-CONTRACT.en.md for the deliberately narrow guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Sequence

from .contracts import Fault, digest, normalize_scope


BACKEND = "linux-bubblewrap-offline-v1"
MIN_BWRAP = (0, 12, 0)  # CVE-2026-87766 fixes sandbox setup symlink traversal.
NAMESPACES = ("user", "pid", "mnt", "net", "ipc", "uts")
PRIVATE_HOME = "/home/xiaolvji"
MAX_TREE_ENTRIES = 200_000
MAX_HOME_FILES = 16
MAX_HOME_BYTES = 2 * 1024 * 1024

# This fixed controller program runs only inside the new user/network
# namespaces.  util-linux preserves capabilities across its exec so lo can be
# configured with an ioctl, without a NETLINK_ROUTE socket.  Clear them before
# execing bubblewrap, which independently creates the remaining namespaces and
# drops all capabilities for the child.  Never run any task-supplied code here.
_LOOPBACK_BOOTSTRAP = r"""
import ctypes,fcntl,os,socket,struct,sys
if os.readlink('/proc/self/ns/net') == sys.argv[1]:
    raise RuntimeError('provider transport requires a new network namespace')
with open('/proc/net/dev') as source:
    interfaces = [line.split(':',1)[0].strip() for line in source if ':' in line]
if interfaces != ['lo']:
    raise RuntimeError('provider transport requires loopback only')
with open('/proc/net/route') as source:
    if any(line.strip() for line in list(source)[1:]):
        raise RuntimeError('provider transport cannot inherit external routes')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as control:
    request = struct.pack('16sh',b'lo',0) + b'\0'*22
    flags = struct.unpack('16sh',fcntl.ioctl(control,0x8913,request)[:18])[1]
    if not flags & 0x8:
        raise RuntimeError('lo is not a loopback interface')
    fcntl.ioctl(control,0x8914,struct.pack('16sh',b'lo',flags|1)+b'\0'*22)
libc = ctypes.CDLL(None,use_errno=True)
header = (ctypes.c_uint32*2)(0x20080522,0)
data = (ctypes.c_uint32*6)()
if libc.capset(ctypes.byref(header),ctypes.byref(data)) != 0:
    raise OSError(ctypes.get_errno(),'provider setup cannot drop capabilities')
with open('/proc/self/status') as source:
    status = dict(line.split(':',1) for line in source if ':' in line)
if any(int(status[name].strip(),16) for name in ('CapInh','CapPrm','CapEff','CapAmb')):
    raise RuntimeError('provider setup retained capabilities')
os.execv(sys.argv[2],sys.argv[2:])
"""

# Native syscall numbers from Linux UAPI, never inferred from libc symbols.
# Compat/x32 ABIs are rejected before these numbers are considered.
_SYSCALLS = {
    "x86_64": {
        "audit_arch": 0xC000003E, "clone": 56, "socket": 41, "socketpair": 53,
        "ioctl": 16,
        "denied": (101, 310, 311, 312, 438, 165, 166, 155, 161, 308, 272,
                   303, 304, 321, 298, 248, 249, 250, 103, 169, 246, 320,
                   175, 176, 313, 172, 173, 167, 168, 323, 133, 259,
                   425, 426, 427, 428, 429, 430, 431, 432, 433, 442),
    },
    "aarch64": {
        "audit_arch": 0xC00000B7, "clone": 220, "socket": 198, "socketpair": 199,
        "ioctl": 29,
        "denied": (117, 270, 271, 272, 438, 40, 39, 41, 51, 268, 97,
                   264, 265, 280, 241, 217, 218, 219, 116, 142, 104, 294,
                   105, 106, 273, 224, 225, 282, 33,
                   425, 426, 427, 428, 429, 430, 431, 432, 433, 442),
    },
}
_TOOLCHAIN = (
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/libexec",
    "/usr/local/bin", "/usr/local/lib", "/usr/local/lib64", "/usr/local/libexec",
    "/usr/share/git-core", "/usr/share/locale", "/usr/share/zoneinfo",
    "/etc/ld.so.cache", "/etc/localtime",
)
_ENV_NAMES = frozenset({"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE",
                        "LC_MESSAGES", "LC_NUMERIC", "LC_TIME", "TERM", "TZ",
                        "NO_COLOR", "CI"})


def _fail(message: str, details=None):
    raise Fault("CAPABILITY_UNSUPPORTED", message, details)


def _canonical(value: str | Path, label: str, *, directory=False) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise Fault("INVALID_SANDBOX_POLICY", f"{label} must be an absolute path.") from exc
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise Fault("INVALID_SANDBOX_POLICY", f"{label} must be absolute without traversal.")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise Fault("INVALID_SANDBOX_POLICY", f"{label} cannot contain symlink components.")
    if not path.exists() or (directory and not path.is_dir()):
        raise Fault("INVALID_SANDBOX_POLICY", f"{label} does not exist with the required type.")
    return path


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _runtime_mounts() -> tuple[list[tuple[Path, Path]], list[tuple[str, Path]]]:
    """One source of truth for implicit host runtime mounts and guest aliases."""
    mounts = []
    aliases = []
    for item in _TOOLCHAIN:
        path = Path(item)
        if path.exists():
            mounts.append((path.resolve(), path))
    for item in ("/bin", "/sbin", "/lib", "/lib64"):
        path = Path(item)
        if path.is_symlink():
            aliases.append((os.readlink(path), path))
        elif path.is_dir():
            mounts.append((path.resolve(), path))
    # Nonstandard prefixes are required mount sources even if currently absent:
    # excluding them would hide a pending read grant from credential validation.
    # A missing required source still makes the eventual launch fail closed.
    for prefix in sorted({Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}, key=str):
        if prefix not in (Path("/usr"), Path("/usr/local")):
            if prefix == Path("/"):
                _fail("The Python runtime cannot require mounting the host root.")
            mounts.append((prefix, prefix))
    return mounts, aliases


def readable_host_sources() -> tuple[Path, ...]:
    """Return unique resolved sources of implicit read-only runtime mounts.

    Credential admission must reject a file equal to or beneath any returned
    path, as read-only mounts still expose bytes to the mapped worker UID. This
    metadata-only query does not inspect file contents, probe tools or start a
    namespace. Optional absent toolchain paths are not mounted; required Python
    prefixes remain included even if absent. Explicit task grants/workspaces are
    separate policy inputs and must also be excluded by their controller.
    """
    mounts, _ = _runtime_mounts()
    return tuple(dict.fromkeys(source for source, _ in mounts))


def _inspect_tree(root: Path) -> None:
    """Reject aliases/special files that a bind mount would otherwise expose."""
    count = 0
    device = root.stat().st_dev
    pending = [root]
    while pending:
        current = pending.pop()
        count += 1
        if count > MAX_TREE_ENTRIES:
            raise Fault("INVALID_SANDBOX_POLICY", "Sandbox source exceeds the bounded entry scan.")
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            try:
                target = current.resolve(strict=False)
            except (RuntimeError, OSError) as exc:
                raise Fault("INVALID_SANDBOX_POLICY", "Sandbox source contains a symlink loop.") from exc
            if not target.is_relative_to(root if root.is_dir() else root.parent):
                raise Fault("INVALID_SANDBOX_POLICY", "Source symlinks must remain inside their granted source tree.")
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise Fault("INVALID_SANDBOX_POLICY", "Source hardlinks are not accepted by the strict backend.")
        elif stat.S_ISDIR(info.st_mode):
            if info.st_dev != device:
                raise Fault("INVALID_SANDBOX_POLICY", "Nested source filesystems are not supported.")
            pending.extend(current.iterdir())
        else:
            raise Fault("INVALID_SANDBOX_POLICY", "Sandbox sources cannot contain sockets, devices or FIFOs.")
    # A same-filesystem bind mount is also an unapproved nested grant.
    if root.is_dir():
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            raw = line.split()[4]
            decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), raw)
            mount = Path(decoded)
            if mount != root and mount.is_relative_to(root):
                raise Fault("INVALID_SANDBOX_POLICY", "Nested source mounts are not supported.")


def _trusted_tool(name: str) -> Path:
    for prefix in (Path("/usr/bin"), Path("/bin")):
        path = (prefix / name).resolve()
        if not path.is_file():
            continue
        info = path.stat()
        if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            _fail(f"The strict backend requires a non-setuid {name} binary.")
        # In an outer user namespace, real root may appear as the overflow UID.
        # Check mutability, rather than incorrectly demanding visible uid zero.
        if os.geteuid() != 0 and any(os.access(item, os.W_OK) for item in (path, *path.parents)
                                    if item != Path("/")):
            _fail(f"The {name} executable and its ancestors must be outside worker write authority.")
        return path
    _fail(f"The strict Linux backend requires the system {name} executable.")


def _sealed_data(name: str, data: bytes) -> int:
    fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        with os.fdopen(os.dup(fd), "wb") as output:
            output.write(data)
        os.lseek(fd, 0, os.SEEK_SET)
        # Linux UAPI values; some otherwise supported Python builds omit these
        # optional names from fcntl even though the running kernel supports them.
        fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x01 | 0x02 | 0x04 | 0x08)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _provider_channel(value: dict) -> tuple[int, dict]:
    """Pin exactly one connected, unnamed stream; never open a host listener.

    Duplication happens before inspection so descriptor reuse cannot substitute
    a different object between validation and launch.  The controller owns the
    peer and its per-attempt authorization; this module validates only the
    transport object, not the broker's application protocol.
    """
    if (not isinstance(value, dict) or set(value) != {"fd"}
            or type(value["fd"]) is not int or value["fd"] < 3):
        raise Fault("INVALID_SANDBOX_POLICY", "Provider transport requires exactly one non-standard descriptor.")
    descriptor = None
    channel = None
    try:
        descriptor = fcntl.fcntl(value["fd"], fcntl.F_DUPFD_CLOEXEC, 3)
        info = os.fstat(descriptor)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Provider transport must be a controller-owned socket")
        channel = socket.socket(fileno=descriptor)
        if (channel.family != socket.AF_UNIX
                or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                or channel.getsockname() not in ("", b"")
                or channel.getpeername() not in ("", b"")):
            raise ValueError("Provider transport must be a connected, unnamed Unix stream pair")
        peer_pid, peer_uid, peer_gid = struct.unpack(
            "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if peer_pid <= 0 or peer_uid != os.geteuid():
            raise ValueError("Provider transport peer must belong to the controller user")
        channel.detach()
        return descriptor, {"kind": "socketpair-v1", "device": info.st_dev, "inode": info.st_ino,
                            "uid": info.st_uid, "gid": info.st_gid,
                            "peer_pid": peer_pid, "peer_uid": peer_uid, "peer_gid": peer_gid,
                            # The trusted bootstrap validates the source FD,
                            # then remaps it to this bridge-only descriptor.
                            "guest_fd": 3}
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        if channel is not None:
            channel.close()
        elif descriptor is not None:
            os.close(descriptor)
        raise Fault("INVALID_SANDBOX_POLICY", "Invalid provider transport: " + str(exc)) from exc


def _private_home_files(value: Sequence[dict]) -> list[tuple[str, bytes, dict]]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_HOME_FILES:
        raise Fault("INVALID_SANDBOX_POLICY", "Private HOME files must be a bounded array.")
    files = []
    paths: list[Path] = []
    total = 0
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"path", "content"}
                or not isinstance(item["path"], str) or not isinstance(item["content"], str)):
            raise Fault("INVALID_SANDBOX_POLICY", "Private HOME files require only path and UTF-8 content.")
        relative = item["path"]
        components = relative.split("/")
        if (not relative or len(relative) > 4096 or "\x00" in relative or "\\" in relative
                or any(part in ("", ".", "..", ".git", "tmp") for part in components)):
            raise Fault("INVALID_SANDBOX_POLICY", "Private HOME paths must be relative without traversal or reserved components.")
        path = Path(relative)
        if path.is_absolute() or any(_overlap(path, previous) for previous in paths):
            raise Fault("INVALID_SANDBOX_POLICY", "Private HOME file paths cannot overlap.")
        try:
            content = item["content"].encode("utf-8")
            if any(len(part.encode("utf-8")) > 255 for part in components):
                raise ValueError("overlong path component")
        except (UnicodeError, ValueError) as exc:
            raise Fault("INVALID_SANDBOX_POLICY", "Private HOME files require valid bounded UTF-8.") from exc
        total += len(content)
        if total > MAX_HOME_BYTES:
            raise Fault("INVALID_SANDBOX_POLICY", "Private HOME content exceeds the aggregate size limit.")
        paths.append(path)
        files.append((relative, content, {"path": relative, "size": len(content), "mode": 0o600,
                                         "sha256": hashlib.sha256(content).hexdigest()}))
    return files


def seccomp_filter(architecture: str | None = None, *, provider_transport: bool = False) -> bytes:
    """Native ABI, dangerous calls denied; IPv4 only with a private provider netns."""
    if type(provider_transport) is not bool:
        raise Fault("INVALID_SANDBOX_POLICY", "Provider seccomp selection must be boolean.")
    architecture = architecture or platform.machine().lower()
    if architecture not in _SYSCALLS:
        _fail("Strict seccomp is implemented only for native x86_64 and aarch64.")
    calls = _SYSCALLS[architecture]
    rows: list[tuple[int, int, int, int]] = []
    def ins(code, k, jt=0, jf=0):
        rows.append((code, jt, jf, k))
    load, equal, bits, ret = 0x20, 0x15, 0x45, 0x06
    deny, allow, kill = 0x00050000 | errno.EPERM, 0x7FFF0000, 0x80000000
    ins(load, 4)  # seccomp_data.arch
    ins(equal, calls["audit_arch"], jt=1)
    ins(ret, kill)
    ins(load, 0)  # seccomp_data.nr
    if architecture == "x86_64":
        ins(0x35, 0x40000000, jf=1)  # x32 shares AUDIT_ARCH_X86_64.
        ins(ret, kill)
    for number in calls["denied"]:
        ins(equal, number, jf=1)
        ins(ret, deny)
    # clone3's pointed-to flags cannot be safely inspected by cBPF.  ENOSYS
    # permits libc to fall back to clone for ordinary threads/subprocesses.
    ins(equal, 435, jf=1)
    ins(ret, 0x00050000 | errno.ENOSYS)
    ins(equal, calls["clone"], jf=4)
    ins(load, 16)  # args[0], low word on the supported little-endian ABIs
    ins(bits, 0x7E020000, jf=1)  # all CLONE_NEW* namespace bits
    ins(ret, deny)
    ins(ret, allow)
    for operation in ("socket", "socketpair"):
        # IPv4 is useful only on lo in the separately created network namespace.
        # AF_INET6, NETLINK, PACKET and VSOCK remain unavailable.  socketpair
        # itself stays Unix-only even in the provider profile.
        families = (1, 2) if provider_transport and operation == "socket" else (1,)
        ins(equal, calls[operation], jf=len(families) + 3)
        ins(load, 16)
        for index, family in enumerate(families):
            ins(equal, family, jt=len(families) - index)
        ins(ret, deny)
        ins(ret, allow)
    ins(equal, calls["ioctl"], jf=5)
    ins(load, 24)  # args[1]
    ins(equal, 0x5412, jt=1)  # TIOCSTI
    ins(equal, 0x541C, jf=1)  # TIOCLINUX
    ins(ret, deny)
    ins(ret, allow)
    ins(ret, allow)
    return b"".join(struct.pack("=HBBI", *row) for row in rows)


@dataclass
class SandboxLaunch:
    argv: list[str]
    env: dict[str, str]
    pass_fds: tuple[int, ...]
    evidence: dict
    provider_fd: int | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self):
        if not self._closed:
            self._closed = True
            for fd in self.pass_fds:
                os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LinuxSandbox:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).absolute()

    def _tools(self):
        if platform.system() != "Linux" or not hasattr(os, "memfd_create"):
            _fail("Strict execution requires Linux namespaces, memfd and seccomp.")
        if sys.byteorder != "little" or platform.machine().lower() not in _SYSCALLS:
            _fail("Strict execution requires a supported native little-endian ABI.")
        bwrap, unshare = _trusted_tool("bwrap"), _trusted_tool("unshare")
        versions = {}
        for name, executable in (("bwrap", bwrap), ("unshare", unshare)):
            try:
                output = subprocess.run([str(executable), "--version"], capture_output=True,
                                        text=True, timeout=3, check=True,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                _fail(f"Cannot verify the system {name} version.", str(exc))
            versions[name] = output
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", versions["bwrap"])
        if not match or tuple(map(int, match.groups())) < MIN_BWRAP:
            _fail("Strict execution requires bubblewrap 0.12.0 or later (CVE-2026-87766).")
        return bwrap, unshare, versions

    def prepare(self, argv: list[str], cwd: str | Path, env: dict[str, str], *,
                workspace: str | Path | None = None, write_scope: list[dict] | None = None,
                readonly_paths: Sequence[str | Path] = (),
                writable_paths: Sequence[str | Path] = (),
                provider_transport: dict | None = None,
                home_files: Sequence[dict] = ()) -> SandboxLaunch:
        bwrap, unshare, versions = self._tools()
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\x00" in x for x in argv) or not argv[0]:
            raise Fault("INVALID_SANDBOX_POLICY", "Command must be a nonempty string array.")
        working = _canonical(cwd, "Working directory", directory=True)
        root = _canonical(workspace or working, "Workspace", directory=True)
        state = self.state_dir.resolve()
        if root == Path("/") or _overlap(root, state) or not working.is_relative_to(root):
            raise Fault("INVALID_SANDBOX_POLICY", "Workspace must be private and disjoint from controller state.")
        _inspect_tree(root)
        if write_scope is not None and not isinstance(write_scope, list):
            raise Fault("INVALID_SANDBOX_POLICY", "Write scope must be null or a scope array.")
        scopes = normalize_scope(root, write_scope) if write_scope is not None else None
        grants = []
        if scopes is not None:
            for scope in scopes:
                path = Path(scope["canonical"])
                if ".git" in Path(scope["path"]).parts:
                    raise Fault("INVALID_SANDBOX_POLICY", "Git administration cannot be a worker write scope.")
                if not path.exists():
                    raise Fault("INVALID_SANDBOX_POLICY", "Scope roots must exist before launch; missing files cannot broaden parent access.")
                grants.append(path)
        ro = [_canonical(item, "Read-only grant") for item in readonly_paths]
        rw = [_canonical(item, "Writable output grant", directory=True) for item in writable_paths]
        for path in ro + rw:
            if path == Path("/") or _overlap(path, state) or _overlap(path, root):
                raise Fault("INVALID_SANDBOX_POLICY", "Additional grants must be disjoint from workspace and controller state.")
            _inspect_tree(path)
        if any(_overlap(a, b) for a in ro for b in rw):
            raise Fault("INVALID_SANDBOX_POLICY", "Additional read/write grants cannot overlap.")

        system_mounts, aliases = _runtime_mounts()
        # Never mount a broader parent to make an executable happen to work.
        for source, destination in system_mounts:
            if _overlap(source, state) or _overlap(source, root) or any(_overlap(source, p) for p in ro + rw):
                raise Fault("INVALID_SANDBOX_POLICY", "A runtime mount overlaps private task/controller data.")
            if any(_overlap(destination, p) for p in [root, *ro, *rw]):
                raise Fault("INVALID_SANDBOX_POLICY", "A task grant overlaps a system runtime mount.")
        reserved = [Path(x) for x in ("/proc", "/dev", PRIVATE_HOME)]
        host_home = Path.home().resolve()
        for path in [root, *ro, *rw]:
            if (any(_overlap(path, item) for item in reserved)
                    or host_home.is_relative_to(path)
                    or path in (Path("/tmp"), Path("/home"), Path("/etc"), Path("/usr"), Path("/opt"))):
                raise Fault("INVALID_SANDBOX_POLICY", "Task grants cannot replace sandbox infrastructure.")
        private_files = _private_home_files(home_files)
        private_directories = sorted(
            {parent for relative, _, _ in private_files
             for parent in Path(relative).parents if parent != Path(".")},
            key=lambda path: (len(path.parts), str(path)))

        child_env = {key: value for key, value in env.items()
                     if key in _ENV_NAMES and isinstance(value, str) and "\x00" not in value}
        child_env.update({
            "HOME": PRIVATE_HOME, "USER": "xiaolvji", "LOGNAME": "xiaolvji",
            "PATH": ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"])),
            "SHELL": "/bin/sh", "PWD": str(working),
            "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp",
            "XDG_CONFIG_HOME": PRIVATE_HOME + "/.config", "XDG_CACHE_HOME": PRIVATE_HOME + "/.cache",
            "XDG_DATA_HOME": PRIVATE_HOME + "/.local/share", "XDG_STATE_HOME": PRIVATE_HOME + "/.local/state",
            "XDG_RUNTIME_DIR": "/tmp/xiaolvji-runtime", "CODEX_HOME": PRIVATE_HOME + "/.codex",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "PYTHONDONTWRITEBYTECODE": "1", "PARALLEL_DEV_ROLE": "worker",
        })
        attempt_marker = env.get("PARALLEL_DEV_ATTEMPT_ID")
        if isinstance(attempt_marker, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", attempt_marker):
            child_env["PARALLEL_DEV_ATTEMPT_ID"] = attempt_marker
        child_env.setdefault("LANG", "C.UTF-8")
        readonly = [str(destination) for _, destination in system_mounts] + [str(x) for x in ro]
        readonly.extend(str(Path(PRIVATE_HOME) / relative) for relative, _, _ in private_files)
        writable = [PRIVATE_HOME, "/tmp", "/dev"]
        writable.extend(str(Path(PRIVATE_HOME) / path) for path in private_directories)
        if scopes is None:
            writable.append(str(root))
        else:
            readonly.append(str(root))
            writable.extend(str(x) for x in grants)
        writable.extend(str(x) for x in rw)
        git_admin = root / ".git"
        if git_admin.exists():
            readonly.append(str(git_admin))
        readonly.extend(["/", "/proc", "/etc/passwd", "/etc/group"])
        policy = {"version": 1, "workspace": str(root),
                  "write_scope": None if scopes is None else [{"path": x["path"], "kind": x["kind"]} for x in scopes],
                  "readonly_paths": [str(x) for x in ro], "writable_paths": [str(x) for x in rw],
                  "network": "none", "readonly_mounts": list(dict.fromkeys(readonly)),
                  "writable_mounts": list(dict.fromkeys(writable)), "namespaces": list(NAMESPACES),
                  "seccomp": "xlj-offline-v1", "architecture": platform.machine().lower(), "home": PRIVATE_HOME}
        if private_files:
            policy["home_files"] = [metadata for _, _, metadata in private_files]
        descriptors = []
        provider_fd = None
        try:
            if provider_transport is not None:
                provider_fd, channel_policy = _provider_channel(provider_transport)
                descriptors.append(provider_fd)
                policy.update({"network": "brokered-provider-v1", "seccomp": "xlj-brokered-v1",
                               "provider_transport": channel_policy})
            filter_fd = _sealed_data("xlj-seccomp", seccomp_filter(provider_transport=provider_fd is not None))
            descriptors.append(filter_fd)
            passwd_fd = _sealed_data("xlj-passwd", f"xiaolvji:x:{os.getuid()}:{os.getgid()}:Sandbox:{PRIVATE_HOME}:/bin/sh\n".encode())
            descriptors.append(passwd_fd)
            group_fd = _sealed_data("xlj-group", f"xiaolvji:x:{os.getgid()}:\n".encode())
            descriptors.append(group_fd)
            # util-linux creates an empty network namespace without configuring
            # loopback via NETLINK_ROUTE, which some outer sandboxes forbid.
            command = [str(unshare), "--user", "--map-current-user", "--net"]
            if provider_fd is not None:
                command.extend(["--keep-caps", sys.executable, "-I", "-S", "-c", _LOOPBACK_BOOTSTRAP,
                                os.readlink("/proc/self/ns/net")])
            command.extend([str(bwrap), "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
                       "--disable-userns", "--cap-drop", "ALL", "--die-with-parent", "--new-session",
                       "--hostname", "xiaolvji"])
            for source, destination in system_mounts:
                command.extend(["--ro-bind", str(source), str(destination)])
            for source, destination in aliases:
                command.extend(["--symlink", source, str(destination)])
            command.extend(["--ro-bind-data", str(passwd_fd), "/etc/passwd",
                            "--ro-bind-data", str(group_fd), "/etc/group",
                            "--tmpfs", "/tmp", "--perms", "0700", "--dir", "/tmp/xiaolvji-runtime",
                            "--tmpfs", PRIVATE_HOME, "--proc", "/proc", "--remount-ro", "/proc", "--dev", "/dev",
                            "--bind" if scopes is None else "--ro-bind", str(root), str(root)])
            # Give each config ancestor its own private mount.  A read-only
            # file alone would not stop renaming a writable ancestor and
            # substituting a different directory at the configured path.
            for path in private_directories:
                command.extend(["--tmpfs", str(Path(PRIVATE_HOME) / path)])
            for relative, content, _ in private_files:
                descriptor = _sealed_data("xlj-private-home", content)
                descriptors.append(descriptor)
                command.extend(["--perms", "0600", "--ro-bind-data", str(descriptor),
                                str(Path(PRIVATE_HOME) / relative)])
            for path in grants + rw:
                command.extend(["--bind", str(path), str(path)])
            if git_admin.exists():
                command.extend(["--ro-bind", str(git_admin), str(git_admin)])
            for path in ro:
                command.extend(["--ro-bind", str(path), str(path)])
            command.extend(["--chdir", str(working), "--remount-ro", "/", "--seccomp", str(filter_fd), "--", *argv])
            return SandboxLaunch(command, child_env, tuple(descriptors),
                                 {"backend": BACKEND, "policy_digest": digest(policy), "policy": policy,
                                  "tool_versions": versions}, provider_fd=provider_fd)
        except BaseException:
            for fd in descriptors:
                os.close(fd)
            raise

    def probe(self) -> dict:
        """A bounded real launch, including actual read/write and kernel checks."""
        evidence = {"backend": BACKEND, "supported": False, "strict_supported": False,
                    "network": "none", "model_network_supported": False,
                    "resource_limits_enforced": False}
        try:
            with tempfile.TemporaryDirectory(prefix="xlj-sandbox-probe-") as temporary:
                parent = Path(temporary)
                work = parent / "work"
                work.mkdir()
                sentinel = parent / "hidden"
                sentinel.write_text("not mounted")
                parents = {name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES}
                script = (
                    "import ctypes,errno,json,os,pathlib,socket; "
                    "status=dict(x.split(':',1) for x in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in x); "
                    "pathlib.Path('proof').write_text('ok'); "
                    "libc=ctypes.CDLL(None,use_errno=True); result=libc.ptrace(0,0,0,0); "
                    "print(json.dumps({'namespaces':{n:os.readlink('/proc/self/ns/'+n) for n in " + repr(NAMESPACES) + "},"
                    "'hidden':os.path.exists(" + repr(str(sentinel)) + "),"
                    "'root_readonly':bool(os.statvfs('/').f_flag&os.ST_RDONLY),"
                    "'home':os.environ['HOME'],'no_new_privs':status['NoNewPrivs'].strip(),"
                    "'seccomp':status['Seccomp'].strip(),'caps':[status[n].strip() for n in ('CapEff','CapPrm','CapBnd')],"
                    "'ptrace_denied':result==-1 and ctypes.get_errno()==errno.EPERM}))"
                )
                with self.prepare([sys.executable, "-I", "-S", "-c", script], work, {}) as launch:
                    result = subprocess.run(launch.argv, env=launch.env, pass_fds=launch.pass_fds,
                                            close_fds=True, capture_output=True, text=True, timeout=8)
                    if result.returncode:
                        _fail("The strict sandbox probe failed.", result.stderr[:2048])
                    observed = json.loads(result.stdout)
                    if not (all(observed["namespaces"][n] != parents[n] for n in NAMESPACES)
                            and observed["hidden"] is False and observed["root_readonly"]
                            and observed["home"] == PRIVATE_HOME and observed["no_new_privs"] == "1"
                            and observed["seccomp"] == "2" and observed["ptrace_denied"]
                            and all(int(x, 16) == 0 for x in observed["caps"])
                            and (work / "proof").read_text() == "ok"):
                        _fail("The sandbox probe did not observe every required boundary.")
                    evidence.update({"supported": True, "strict_supported": True,
                                     "observed": observed, "tool_versions": launch.evidence["tool_versions"],
                                     "blocking_reasons": []})
        except (Fault, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            evidence["blocking_reasons"] = [str(exc)]
            if isinstance(exc, Fault) and exc.details:
                evidence["details"] = exc.details
        return evidence
