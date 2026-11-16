"""Linux child-subreaper for one owned attempt.

This trusted program stays outside the worker boundary. Cooperative commands
use a child subreaper; controlled commands additionally use a verified Linux
sandbox. An isolated bootstrap reports the installed boundary and waits until
its attestation is durably recorded before executing any task code. A killed
supervisor without a durable completion remains uncertain in either mode.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_BOOTSTRAP_BYTES = 256 * 1024
_cancel_signal = False

# This is trusted code passed directly to an isolated interpreter. No task
# module, workspace startup file, site hook, or controller path is imported.
# The command and its input arrive through a pipe, never a readable host file.
# Only the first stdout line is a control message; task stdout is treated as
# opaque log bytes after release, so the worker cannot manufacture attestations.
_SANDBOX_BOOTSTRAP = r'''
import fcntl, hashlib, json, os, signal, socket, struct, sys, tempfile
try:
    raw = sys.stdin.buffer.readline(4 * 1024 * 1024 + 1)
    if not raw or len(raw) > 4 * 1024 * 1024:
        raise ValueError("invalid bootstrap input")
    request = json.loads(raw)
    status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
    namespaces = {name: os.readlink("/proc/self/ns/" + name) for name in request["namespaces"]}
    mounts = {}
    for path in request["readonly_mounts"] + request["writable_mounts"]:
        mounts[path] = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    with open("/proc/self/mountinfo", "rb") as stream:
        mountinfo = stream.read(1024 * 1024 + 1)
    if len(mountinfo) > 1024 * 1024:
        raise ValueError("mount information exceeds bound")
    report = {"protocol": "xiaolvji-sandbox-bootstrap-v1", "nonce": request["nonce"],
              "policy_digest": request["policy_digest"], "pid": os.getpid(), "ppid": os.getppid(),
              "cwd": os.getcwd(), "namespaces": namespaces, "mount_flags": mounts,
              "mountinfo_sha256": hashlib.sha256(mountinfo).hexdigest(),
              "status": {key: status.get(key, "").strip() for key in
                         ("CapEff", "CapPrm", "CapInh", "CapAmb", "CapBnd", "NoNewPrivs", "Seccomp")},
              "controller_paths_hidden": all(not os.path.lexists(path) for path in request["hidden_paths"])}
    if request.get("provider_fd") is not None:
        endpoint = socket.socket(fileno=request["provider_fd"])
        info = os.fstat(endpoint.fileno())
        peer = struct.unpack("3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        report["provider_transport"] = {"device": info.st_dev, "inode": info.st_ino,
            "uid": info.st_uid, "gid": info.st_gid, "family": endpoint.family,
            "type": endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE),
            "peer_pid": peer[0], "peer_uid": peer[1], "peer_gid": peer[2],
            "unnamed": endpoint.getsockname() == "" and endpoint.getpeername() == ""}
        endpoint.detach()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as query:
            flags = fcntl.ioctl(query.fileno(), 0x8913, struct.pack("16sH14s", b"lo", 0, b""))
        interfaces = [line.split(":", 1)[0].strip() for line in open("/proc/net/dev") if ":" in line]
        routes = [line.strip() for line in open("/proc/net/route") if line.strip()][1:]
        report["provider_network"] = {"interfaces": interfaces, "loopback_up": bool(struct.unpack("16sH14s", flags)[1] & 1),
                                      "external_ipv4_routes": len(routes)}
    encoded = (json.dumps(report, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 256 * 1024:
        raise ValueError("bootstrap report exceeds bound")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    release = sys.stdin.buffer.readline(256)
    if release != (request["nonce"] + "\n").encode():
        os._exit(125)
    # Replace the control pipe before exec. No launch descriptor or controller
    # input survives into task code; its stdin contains only its own input.
    task_input = tempfile.TemporaryFile()
    task_input.write(request["stdin_text"].encode("utf-8"))
    task_input.seek(0)
    os.dup2(task_input.fileno(), 0)
    retained = 2
    if request.get("provider_fd") is not None:
        os.dup2(request["provider_fd"], 3, inheritable=False)
        retained = 3
    for descriptor in os.listdir("/proc/self/fd"):
        if descriptor.isdigit() and int(descriptor) > retained:
            try:
                os.close(int(descriptor))
            except OSError:
                pass
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    if request.get("provider_fd") is not None:
        bridge = {"__name__": "xiaolvji_isolated_provider_bridge"}
        exec(compile(request["provider_bridge_source"], "<xiaolvji-provider-bridge>", "exec"), bridge)
        result = bridge["run_bridge"](request["argv"], request["provider_runtime"], 3)
        os._exit(result if result >= 0 else 128 - result)
    os.execvpe(request["argv"][0], request["argv"], dict(os.environ))
except BaseException:
    # Do not serialize argv, input, environment, or exception values into a
    # controller log. Their contents can include task data or credentials.
    os.write(2, b"xiaolvji: isolated bootstrap or command launch failed\n")
    os._exit(125)
'''


class SandboxSetupError(RuntimeError):
    pass


def _namespace_names() -> dict[str, str]:
    return {name: os.readlink("/proc/self/ns/" + name)
            for name in ("user", "pid", "mnt", "net", "ipc", "uts")}


def _log_chunk(name: str, chunk: bytes, files: dict, written: dict, truncated: dict) -> None:
    remaining = MAX_LOG_BYTES - written[name]
    if remaining > 0:
        files[name].write(chunk[:remaining])
        written[name] += min(len(chunk), remaining)
    if len(chunk) > remaining:
        truncated[name] = True


def _bootstrap_exchange(worker, request: dict, directory: Path, deadline: float,
                        files: dict, written: dict, truncated: dict) -> dict:
    """Bounded, cancellable I/O while untrusted command execution is gated."""
    pending = memoryview((json.dumps(request, separators=(",", ":")) + "\n").encode())
    if len(pending) > 4 * 1024 * 1024:
        raise SandboxSetupError("bootstrap_request_too_large")
    received = bytearray()
    selector = selectors.DefaultSelector()
    for stream in (worker.stdin, worker.stdout, worker.stderr):
        os.set_blocking(stream.fileno(), False)
    selector.register(worker.stdin, selectors.EVENT_WRITE, "stdin")
    selector.register(worker.stdout, selectors.EVENT_READ, "stdout")
    selector.register(worker.stderr, selectors.EVENT_READ, "stderr")
    try:
        while time.monotonic() < deadline:
            if _cancel_signal or (directory / "cancel.json").exists():
                raise SandboxSetupError("cancelled_before_worker_authorization")
            for key, _ in selector.select(timeout=0.025):
                if key.data == "stdin":
                    try:
                        count = os.write(worker.stdin.fileno(), pending[:65536])
                    except (BrokenPipeError, OSError) as exc:
                        raise SandboxSetupError("sandbox_bootstrap_input_closed") from exc
                    pending = pending[count:]
                    if not pending:
                        selector.unregister(worker.stdin)
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise SandboxSetupError("sandbox_bootstrap_exited_before_attestation")
                    continue
                if key.data == "stderr":
                    _log_chunk("stderr", chunk, files, written, truncated)
                    continue
                received.extend(chunk)
                if len(received) > MAX_BOOTSTRAP_BYTES:
                    raise SandboxSetupError("sandbox_attestation_too_large")
                if b"\n" in received:
                    line, remainder = received.split(b"\n", 1)
                    if pending or remainder:
                        raise SandboxSetupError("sandbox_bootstrap_protocol_violation")
                    try:
                        result = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        raise SandboxSetupError("sandbox_attestation_invalid_json") from exc
                    if not isinstance(result, dict):
                        raise SandboxSetupError("sandbox_attestation_invalid_json")
                    return result
        raise SandboxSetupError("sandbox_attestation_timeout")
    finally:
        selector.close()


def _validate_attestation(report: dict, request: dict, parent_namespaces: dict,
                          cwd: str, evidence: dict, launcher: dict) -> tuple[dict, int]:
    """Bind bootstrap claims to owned kernel identities before gate release."""
    if (report.get("protocol") != "xiaolvji-sandbox-bootstrap-v1" or
            report.get("nonce") != request["nonce"] or report.get("policy_digest") != evidence["policy_digest"] or
            report.get("cwd") != cwd or report.get("controller_paths_hidden") is not True or
            type(report.get("pid")) is not int or report["pid"] <= 1 or report.get("ppid") != 1):
        raise SandboxSetupError("sandbox_attestation_identity_mismatch")
    namespaces = report.get("namespaces", {})
    if set(namespaces) != set(parent_namespaces) or any(
            not isinstance(value, str) or value == parent_namespaces[name]
            for name, value in namespaces.items()):
        raise SandboxSetupError("sandbox_namespace_isolation_missing")
    status = report.get("status", {})
    try:
        no_caps = all(int(status[name], 16) == 0 for name in ("CapEff", "CapPrm", "CapInh", "CapAmb", "CapBnd"))
    except (KeyError, TypeError, ValueError):
        no_caps = False
    if not no_caps or status.get("NoNewPrivs") != "1" or status.get("Seccomp") != "2":
        raise SandboxSetupError("sandbox_privilege_restrictions_missing")
    flags = report.get("mount_flags", {})
    if any(flags.get(path) is not True for path in request["readonly_mounts"]) or any(
            flags.get(path) is not False for path in request["writable_mounts"]):
        raise SandboxSetupError("sandbox_mount_policy_mismatch")
    channel = evidence["policy"].get("provider_transport")
    if channel is not None:
        observed = report.get("provider_transport", {})
        if (any(observed.get(key) != channel[key] for key in ("device", "inode", "uid", "gid", "peer_uid", "peer_gid")) or
                observed.get("family") != 1 or observed.get("type") != 1 or observed.get("unnamed") is not True or
                observed.get("peer_pid") != 0):
            raise SandboxSetupError("sandbox_provider_channel_mismatch")
        network = report.get("provider_network", {})
        if network.get("interfaces") != ["lo"] or network.get("loopback_up") is not True or network.get("external_ipv4_routes") != 0:
            raise SandboxSetupError("sandbox_provider_network_mismatch")
    elif "provider_transport" in report or "provider_network" in report:
        raise SandboxSetupError("sandbox_unexpected_provider_transport")
    init = bootstrap = None
    for item in descendants(os.getpid()):
        try:
            proc = Path("/proc") / str(item["proc_pid"])
            if any(os.readlink(proc / "ns" / name) != value for name, value in namespaces.items()):
                continue
            fields = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
            nested_pid = int(fields["NSpid"].split()[-1])
            if nested_pid == 1:
                init = item
            elif nested_pid == report["pid"]:
                bootstrap = item
        except (OSError, ValueError, KeyError, IndexError):
            continue
    if not init or not bootstrap or bootstrap["proc_ppid"] != init["proc_pid"]:
        raise SandboxSetupError("sandbox_namespace_identity_unconfirmed")
    init_fd = os.pidfd_open(init["pid"], 0)
    current = process_identity(init["pid"])
    if not current or current["start_time"] != init["start_time"] or select.select([init_fd], [], [], 0)[0]:
        os.close(init_fd)
        raise SandboxSetupError("sandbox_init_identity_changed")
    actual = {key: value for key, value in report.items() if key not in {"nonce", "protocol", "policy_digest"}}
    attestation = {**evidence, "attested": True, "actual": actual,
                   "launcher_identity": launcher, "init_identity": init, "bootstrap_identity": bootstrap}
    return attestation, init_fd


def _proc_identity(proc_pid: int) -> dict[str, Any] | None:
    try:
        data = Path(f"/proc/{proc_pid}/stat").read_text()
        rest = data[data.rfind(")") + 2:].split()
        return {"proc_pid": proc_pid, "start_time": rest[19], "proc_ppid": int(rest[1]),
                "state": rest[0]}
    except (OSError, ValueError, IndexError):
        return None


def process_identity(pid: int) -> dict[str, Any] | None:
    """Map namespace-local PID through pidfd to the mounted /proc PID view.

Some containers expose an ancestor namespace's /proc. Looking up /proc/<pid>
directly in that situation may inspect an unrelated process. fdinfo's Pid is
expressed in the procfs mount's namespace and pins the intended process.
    """
    try:
        fd = os.pidfd_open(pid, 0)
        try:
            fields = dict(line.split(":", 1) for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines() if ":" in line)
            proc_pid = int(fields["Pid"].strip())
            identity = _proc_identity(proc_pid)
            if not identity:
                return None
            return {**identity, "pid": pid, "session_id": os.getsid(pid),
                    "pid_namespace": os.readlink("/proc/self/ns/pid")}
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError):
        return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def atomic_json(path: Path, value: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, sort_keys=True, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp.exists():
            tmp.unlink()


def descendants(pid: int) -> list[dict[str, Any]]:
    root = process_identity(pid)
    if not root:
        raise RuntimeError("supervisor identity unavailable")
    namespace_fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    namespace_depth = len(namespace_fields["NSpid"].split())
    found: dict[int, dict[str, Any]] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            item = _proc_identity(int(entry.name))
            if item:
                found[item["proc_pid"]] = item
    owned = {root["proc_pid"]}
    result: list[dict[str, Any]] = []
    while True:
        added = [item for child, item in found.items() if child not in owned and item["proc_ppid"] in owned]
        if not added:
            converted = []
            for item in reversed(result):
                try:
                    status = Path(f"/proc/{item['proc_pid']}/status").read_text()
                    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
                    local_pid = int(fields["NSpid"].split()[namespace_depth - 1])
                    current = process_identity(local_pid)
                    if current and current["proc_pid"] == item["proc_pid"] and current["start_time"] == item["start_time"]:
                        converted.append(current)
                except (OSError, ValueError, KeyError, IndexError):
                    continue
            return converted
        result.extend(added)
        owned.update(item["proc_pid"] for item in added)


def signal_identity(identity: dict[str, Any], sig: int) -> bool:
    """Use pidfd to avoid signalling a recycled PID between check and signal."""
    try:
        fd = os.pidfd_open(identity["pid"], 0)
    except ProcessLookupError:
        return False
    try:
        current = process_identity(identity["pid"])
        if not current or current["start_time"] != identity["start_time"]:
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(fd)


def _request_cancel(signum: int, frame: Any) -> None:
    global _cancel_signal
    _cancel_signal = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--profile", choices=("cooperative_native", "controlled_executor"), default="cooperative_native")
    parser.add_argument("--policy-digest", default="")
    args = parser.parse_args()
    directory = Path(args.directory)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        return 74
    signal.signal(signal.SIGTERM, _request_cancel)
    signal.signal(signal.SIGINT, _request_cancel)
    current = process_identity(os.getpid())
    if not current:
        return 74
    identity = {"attempt_id": args.attempt, "generation": args.generation, "nonce": args.nonce,
                "pid": current["pid"], "start_time": current["start_time"],
                "boot_id": boot_id(), "session_id": current["session_id"],
                "proc_pid": current["proc_pid"], "pid_namespace": current["pid_namespace"],
                "tracking": "linux_child_subreaper_v1", "profile": args.profile,
                "sandbox_policy_digest": args.policy_digest or None}
    atomic_json(directory / "identity.json", identity)
    # Nothing capable of executing task code is created until the controller
    # durably records this identity and sends the matching nonce.
    raw = sys.stdin.buffer.readline(4 * 1024 * 1024 + 1)
    if not raw or len(raw) > 4 * 1024 * 1024:
        atomic_json(directory / "result.json", {"identity": identity, "exit_code": 125,
                    "process_tree_terminated": True, "no_process_created": True,
                    "worker_started": False, "cancelled": False, "timed_out": False, "reason": "launch_not_authorized"})
        return 0
    try:
        spec = json.loads(raw)
        if (spec["nonce"] != args.nonce or spec.get("profile", "cooperative_native") != args.profile or
                spec.get("sandbox_policy_digest") != (args.policy_digest or None)):
            raise ValueError("nonce mismatch")
        argv, cwd, env = spec["argv"], spec["cwd"], spec["env"]
        timeout, grace = float(spec["timeout"]), float(spec.get("grace", 1.0))
        if timeout <= 0 or grace < 0:
            raise ValueError("invalid timeout")
    except (KeyError, ValueError, TypeError):
        atomic_json(directory / "result.json", {"identity": identity, "exit_code": 125,
                    "process_tree_terminated": True, "no_process_created": True,
                    "worker_started": False, "cancelled": False, "timed_out": False, "reason": "invalid_launch_authorization"})
        return 0
    if _cancel_signal or (directory / "cancel.json").exists():
        atomic_json(directory / "result.json", {"identity": identity, "exit_code": 125,
                    "process_tree_terminated": True, "no_process_created": True,
                    "worker_started": False, "cancelled": True, "timed_out": False, "reason": "cancelled_before_launch"})
        return 0
    output = {"stdout": directory / "stdout.log", "stderr": directory / "stderr.log"}
    files = {name: open(path, "wb") for name, path in output.items()}
    written = {"stdout": 0, "stderr": 0}
    truncated = {"stdout": False, "stderr": False}
    input_file = None
    sandbox_evidence = None
    provider_fd = None
    selector = selectors.DefaultSelector()
    worker = None
    try:
        if args.profile == "controlled_executor":
            # This import comes only from the fixed controller installation,
            # never from the task checkout or inherited Python path.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from xiaolvji.contracts import digest
            from xiaolvji.sandbox import LinuxSandbox
            policy = spec.get("sandbox_policy") or {}
            with LinuxSandbox(directory.parent.parent).prepare(
                    [sys.executable, "-I", "-S", "-c", _SANDBOX_BOOTSTRAP], cwd, env, **policy) as launch:
                sandbox_evidence = launch.evidence
                provider_fd = launch.provider_fd
                if (sandbox_evidence["policy_digest"] != args.policy_digest or
                        digest(sandbox_evidence["policy"]) != args.policy_digest):
                    raise SandboxSetupError("sandbox_policy_changed_before_launch")
                worker = subprocess.Popen(launch.argv, cwd=cwd, env=launch.env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          close_fds=True, pass_fds=launch.pass_fds, start_new_session=True)
            if policy.get("provider_transport") is not None:
                # The child owns its pinned duplicate. Keeping the original in
                # this outside supervisor would mask an IPC peer disconnect.
                os.close(policy["provider_transport"]["fd"])
        else:
            input_file = tempfile.TemporaryFile()
            input_file.write(spec.get("stdin_text", "").encode("utf-8"))
            input_file.seek(0)
            worker = subprocess.Popen(argv, cwd=cwd, env=env, stdin=input_file,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      close_fds=True, start_new_session=True)
    except Exception as exc:
        if worker is not None:
            # A descriptor/context failure after Popen must never become a
            # no-process attestation. Best-effort cleanup does not prove exit.
            for child in descendants(os.getpid()):
                try:
                    signal_identity(child, signal.SIGKILL)
                except OSError:
                    pass
        atomic_json(directory / "result.json", {"identity": identity, "exit_code": 127,
                    "process_tree_terminated": worker is None, "no_process_created": worker is None,
                    "worker_started": worker is not None and args.profile == "cooperative_native",
                    "cancelled": False, "timed_out": False,
                    "reason": "launch_failed:" + (str(exc.errno) if isinstance(exc, OSError) else type(exc).__name__),
                    "stdout_path": str(output["stdout"]), "stderr_path": str(output["stderr"])})
        for out in files.values():
            out.close()
        if input_file:
            input_file.close()
        return 0 if worker is None else 74
    worker_identity = process_identity(worker.pid)
    atomic_json(directory / "worker.json", {"identity": identity, "worker": worker_identity})
    started = time.monotonic()
    stop_started: float | None = None
    cancelled = timed_out = False
    primary_exit: int | None = None
    terminated = False
    reason = None
    sandbox = None
    init_fd = None
    init_exited = False
    worker_started = args.profile == "cooperative_native"
    if args.profile == "controlled_executor":
        try:
            if not worker_identity or not sandbox_evidence:
                raise SandboxSetupError("sandbox_launcher_identity_unavailable")
            policy = sandbox_evidence["policy"]
            parent_namespaces = _namespace_names()
            request = {"nonce": args.nonce, "argv": argv, "stdin_text": spec.get("stdin_text", ""),
                       "policy_digest": sandbox_evidence["policy_digest"], "namespaces": list(parent_namespaces),
                       "readonly_mounts": policy["readonly_mounts"], "writable_mounts": policy["writable_mounts"],
                       "hidden_paths": [str(directory), str(directory.parent), str(directory / "identity.json")]}
            if provider_fd is not None:
                request.update({"provider_fd": provider_fd, "provider_runtime": spec["provider_runtime"],
                    "provider_bridge_source": Path(__file__).with_name("provider_bridge.py").read_text(encoding="utf-8")})
            report = _bootstrap_exchange(worker, request, directory, started + min(timeout, 10), files, written, truncated)
            sandbox, init_fd = _validate_attestation(report, request, parent_namespaces, cwd, sandbox_evidence, worker_identity)
            # This durable record cannot be reached by the isolated bootstrap
            # or its future command. Only after the record is fsynced may task
            # code run, even if the controller crashes at this transition.
            atomic_json(directory / "sandbox.json", sandbox)
            if _cancel_signal or (directory / "cancel.json").exists():
                raise SandboxSetupError("cancelled_before_worker_authorization")
            release = (args.nonce + "\n").encode()
            count = os.write(worker.stdin.fileno(), release)
            if count != len(release):
                raise SandboxSetupError("sandbox_authorization_incomplete")
            worker_started = True
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, SandboxSetupError) else "sandbox_setup_failed:" + type(exc).__name__
        finally:
            worker.stdin.close()
    for stream, name in ((worker.stdout, "stdout"), (worker.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        while True:
            for key, _ in selector.select(timeout=0.025):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                _log_chunk(name, chunk, files, written, truncated)
            no_children = False
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    no_children = True
                    break
                if pid == 0:
                    break
                if pid == worker.pid:
                    primary_exit = os.waitstatus_to_exitcode(status)
                    worker.returncode = primary_exit
            now = time.monotonic()
            if now - started >= timeout:
                timed_out = True
            if no_children:
                init_exited = bool(init_fd is not None and select.select([init_fd], [], [], 0)[0])
                terminated = args.profile != "controlled_executor" or not worker_started or init_exited
                if not terminated:
                    reason = "sandbox_init_exit_unconfirmed"
                break
            cancel_path = directory / "cancel.json"
            if _cancel_signal or cancel_path.exists():
                cancelled = True
                if cancel_path.exists():
                    try:
                        request = json.loads(cancel_path.read_text())
                        if request.get("nonce") == args.nonce:
                            grace = max(0.0, min(30.0, float(request.get("grace", grace))))
                    except (OSError, ValueError, TypeError):
                        pass
            # Even a successful primary process may leave a detached daemon.
            # Such descendants must terminate before the completion is valid.
            if primary_exit is not None or cancelled or timed_out or reason is not None:
                if stop_started is None:
                    stop_started = now
                sig = signal.SIGKILL if now - stop_started >= grace else signal.SIGTERM
                for child in descendants(os.getpid()):
                    signal_identity(child, sig)
                if now - stop_started > grace + 10:
                    reason = "descendant_termination_unconfirmed"
                    break
    except BaseException as exc:
        reason = f"supervisor_error:{type(exc).__name__}"
        # Do not manufacture an exit attestation after an unexpected error.
        # Best effort cleanup is still performed with identity-safe pidfds.
        for child in descendants(os.getpid()):
            try:
                signal_identity(child, signal.SIGKILL)
            except OSError:
                pass
    finally:
        # Drain data remaining after all descendants exited, bounded by the same
        # file cap. No content is printed into controller diagnostics.
        for key in list(selector.get_map().values()):
            while True:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                name = key.data
                _log_chunk(name, chunk, files, written, truncated)
            key.fileobj.close()
        selector.close()
        for out in files.values():
            out.flush()
            os.fsync(out.fileno())
            out.close()
        if input_file:
            input_file.close()
        if init_fd is not None:
            os.close(init_fd)
    result = {"identity": identity, "exit_code": primary_exit if primary_exit is not None else 125,
              "process_tree_terminated": terminated, "cancelled": cancelled, "timed_out": timed_out,
              "worker_started": worker_started,
              "stdout_path": str(output["stdout"]), "stderr_path": str(output["stderr"]),
              "log_truncated": truncated, "duration_seconds": time.monotonic() - started}
    if sandbox is not None:
        result["sandbox"] = sandbox
        result["termination_evidence"] = {"kind": "linux_pid_namespace_and_subreaper_v1",
                                          "namespace_init_exited": init_exited,
                                          "all_children_reaped": terminated}
    if args.profile == "controlled_executor" and not worker_started and result["exit_code"] == 0:
        result["exit_code"] = 125
    if reason:
        result["reason"] = reason
    atomic_json(directory / "result.json", result)
    return 0 if terminated else 74


if __name__ == "__main__":
    raise SystemExit(main())
