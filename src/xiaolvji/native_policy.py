"""Explicit standalone deployments and bounded native identity capture.

A deployment descriptor is an operator's configuration choice, not an assertion
that a host passed integration tests. It does not import an existing account,
user HOME, credentials, hooks, or cloud-managed policy. The initial Codex profile
requires the host's system policy root to be absent; policy transfer is not yet
implemented. Absence is checked on the controller host before native startup.

Identity capture does not run the selected host executable. Native protocol and
tool-execution acceptance must be established separately inside the attested
executor. ZCode descriptors can be validated, but production identity capture is
blocked until its complete runtime distribution and policy scope are supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile

from .contracts import Fault, canonical_json, digest, load_json


MAX_DESCRIPTOR_BYTES = 65_536
MAX_NATIVE_BYTES = 1024 * 1024 * 1024
MAX_RUNTIME_FILE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_FILES = 4096
MAX_PROJECT_POLICY_ENTRIES = 100_000
MAX_PROJECT_POLICY_DEPTH = 128
MAX_INSTRUCTION_BYTES = 1024 * 1024
MAX_PROJECT_INSTRUCTION_BYTES = 16 * 1024 * 1024
MAX_PROJECT_INSTRUCTION_FILES = 4096
MAX_INSTRUCTION_INDEX_BYTES = 1024 * 1024
_ELF_MACHINE = {"x86_64": 62, "aarch64": 183}
_PROTOCOLS = {"codex": "codex-app-server-v2", "zcode": "zcode-cli-v1"}
_CODEX_POLICY_ROOTS = (("codex_system_policy", Path("/etc/codex")),)


def _unsupported(message: str):
    raise Fault("CAPABILITY_UNSUPPORTED", message)


def _invalid(message: str):
    raise Fault("INVALID_NATIVE_DEPLOYMENT", message)


def _absolute_path(value) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or
            len(value) > 4096 or "\x00" in value or
            any(part in {"", ".", ".."} for part in value.split("/")[1:])):
        _invalid("Native deployment executable must be a canonical absolute file path.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid("Native deployment executable must contain valid Unicode text.")
    return value


@dataclass(frozen=True)
class NativeDeployment:
    _json: str = field(repr=False)

    @property
    def value(self) -> dict:
        try:
            if not isinstance(self._json, str):
                _invalid("Native deployment must contain validated JSON text.")
            return load_json(self._json, limit=MAX_DESCRIPTOR_BYTES)
        except Fault:
            _invalid("Native deployment must contain validated JSON text.")

    @property
    def digest(self) -> str:
        return digest(self.value)


def normalize_native_deployment(value) -> NativeDeployment:
    """Normalize only the closed deployment schema, without inspecting files."""
    try:
        value = load_json(canonical_json(value), limit=MAX_DESCRIPTOR_BYTES)
    except Fault:
        _invalid("Native deployment must be bounded, unambiguous JSON data.")
    fields = {"schema_version", "engine", "executable", "protocol", "policy"}
    if not isinstance(value, dict) or set(value) != fields:
        _invalid("Native deployment must match the closed version-one schema.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _invalid("Unsupported native deployment schema version.")
    engine = value["engine"]
    if not isinstance(engine, str) or engine not in _PROTOCOLS:
        _invalid("Native deployment engine must be Codex or ZCode.")
    if value["protocol"] != _PROTOCOLS[engine]:
        _invalid("Native deployment protocol does not match its engine.")
    _absolute_path(value["executable"])
    expected_policy = {"context": "standalone-api", "system_policy": "must-be-absent"}
    expected_policy.update({"approval_policy": "on-request"} if engine == "codex" else {"mode": "guarded"})
    if not isinstance(value["policy"], dict) or value["policy"] != expected_policy:
        _invalid("Native deployment requires the exact supported standalone policy.")
    return NativeDeployment(canonical_json(value))


def _regular_bytes(path, *, maximum: int, label: str,
                   dir_fd: int | None = None, expected_identity=None) -> bytes:
    """Read a small regular file without path aliases or changing contents."""
    descriptor = None
    try:
        path = Path(path)
        if dir_fd is None:
            _check_path_components(path, label)
        elif path.is_absolute() or len(path.parts) != 1 or path.name in (".", ".."):
            _unsupported(label + " must be one directory-relative file name.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            _unsupported(label + " is not a supported bounded regular file.")
        if expected_identity is not None and _stat_identity(before) != expected_identity:
            _unsupported(label + " changed before its contents could be captured.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after) or len(data) != before.st_size:
            _unsupported(label + " changed while its identity was captured.")
        if len(data) > maximum:
            _unsupported(label + " exceeds the identity capture budget.")
        if dir_fd is None:
            _check_path_components(path, label)
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if _stat_identity(named) != _stat_identity(after):
            _unsupported(label + " was replaced during identity capture.")
        return data
    except (OSError, TypeError, ValueError, RuntimeError):
        _unsupported(label + " is unavailable or unsafe for identity capture.")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_native_deployment(path: str | Path) -> NativeDeployment:
    """Load an explicitly named descriptor; never discover user configuration."""
    try:
        path = Path(path).absolute()
    except (TypeError, ValueError, OSError):
        _invalid("Native deployment descriptor path is invalid.")
    raw = _regular_bytes(path, maximum=MAX_DESCRIPTOR_BYTES, label="Native deployment descriptor")
    try:
        value = load_json(raw, limit=MAX_DESCRIPTOR_BYTES)
    except Fault:
        _invalid("Native deployment file is not bounded, unambiguous JSON data.")
    return normalize_native_deployment(value)


def _stat_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _check_path_components(path: Path, label: str):
    if not path.is_absolute() or ".." in path.parts:
        _unsupported(label + " must use an absolute path without aliases.")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            _unsupported(label + " cannot use symbolic links.")
        if component != path and not stat.S_ISDIR(info.st_mode):
            _unsupported(label + " has an unsupported path component.")


def _system_policy_inventory(engine: str) -> dict:
    # A directory's mere presence blocks this initial profile. No configuration
    # contents or directory entries are read, even if the directory is empty.
    # ZCode's inspected loader has no documented machine-policy file path; do
    # not invent an /etc/zcode absence claim for that unsupported deployment.
    if engine != "codex":
        _unsupported("The ZCode machine-policy scope is not supported by native identity capture.")
    checked = []
    for name, path in _CODEX_POLICY_ROOTS:
        try:
            for parent in reversed(path.parents):
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    _unsupported("The system policy inventory contains an unsupported path alias.")
            try:
                path.lstat()
            except FileNotFoundError:
                checked.append({"source": name, "status": "absent"})
            else:
                _unsupported("An applicable system policy source is present; native policy transfer is not implemented.")
        except (OSError, ValueError, RuntimeError):
            _unsupported("The applicable system policy inventory could not be established.")
    return {"profile": "known-system-sources-absent-v1", "sources": checked}


def _read_at(descriptor: int, offset: int, size: int, total: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > total:
        _unsupported("Native executable contains an invalid ELF file range.")
    data = os.pread(descriptor, size, offset)
    if len(data) != size:
        _unsupported("Native executable changed or has an incomplete ELF header.")
    return data


def _elf_identity(descriptor: int, size: int, machine: str, *, require_static: bool) -> dict:
    header = _read_at(descriptor, 0, 64, size)
    if (header[:4] != b"\x7fELF" or header[4] != 2 or
            header[5] != (1 if sys.byteorder == "little" else 2) or header[6] != 1):
        _unsupported("Native identity requires a 64-bit ELF executable, not a script or wrapper.")
    endian = "<" if header[5] == 1 else ">"
    values = struct.unpack(endian + "16sHHIQQQIHHHHHH", header)
    _, kind, arch, version, _, program_offset, _, _, header_size, entry_size, count, _, _, _ = values
    if (kind not in (2, 3) or version != 1 or header_size != 64 or
            arch != _ELF_MACHINE.get(machine) or entry_size != 56 or not 1 <= count <= 1024):
        _unsupported("Native executable ELF identity is unsupported on this machine.")
    table = _read_at(descriptor, program_offset, count * entry_size, size)
    interpreter = False
    dependencies = False
    for offset in range(0, len(table), entry_size):
        segment_type, _, file_offset, _, _, file_size, _, _ = struct.unpack(
            endian + "IIQQQQQQ", table[offset:offset + entry_size])
        if file_offset + file_size > size:
            _unsupported("Native executable contains an invalid ELF segment.")
        interpreter = interpreter or segment_type == 3  # PT_INTERP
        if segment_type == 2:  # PT_DYNAMIC in static PIE too.
            if file_size > 1024 * 1024 or file_size % 16:
                _unsupported("Native executable has an unsupported dynamic table.")
            dynamic = _read_at(descriptor, file_offset, file_size, size)
            for position in range(0, len(dynamic), 16):
                tag, _ = struct.unpack(endian + "qQ", dynamic[position:position + 16])
                if tag == 0:
                    break
                if tag in (1, 0x7FFFFFFD, 0x7FFFFFFF):  # NEEDED, AUXILIARY, FILTER
                    dependencies = True
    if require_static and (interpreter or dependencies):
        _unsupported("This native profile requires a static Codex ELF; external runtime dependency capture is not implemented.")
    return {"format": "elf64", "architecture": machine,
            "static_runtime": not interpreter and not dependencies}


def _file_identity(path: Path, *, maximum: int, label: str,
                   machine: str | None = None, require_static: bool = False) -> dict:
    descriptor = None
    try:
        _check_path_components(path, label)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum:
            _unsupported(label + " is not a supported bounded regular file.")
        extra = {}
        if machine is not None:
            if (before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH) or
                    before.st_mode & 0o111 == 0 or not os.access(path, os.X_OK)):
                _unsupported(label + " has unsupported executable permissions.")
            extra = _elf_identity(descriptor, before.st_size, machine, require_static=require_static)
        hasher = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                _unsupported(label + " exceeds the identity capture budget.")
            hasher.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after) or count != before.st_size:
            _unsupported(label + " changed while its identity was captured.")
        # Verify that the selected name still denotes the object we hashed.
        _check_path_components(path, label)
        if _stat_identity(path.lstat()) != _stat_identity(after):
            _unsupported(label + " was replaced during identity capture.")
        return {"path": str(path), "sha256": "sha256:" + hasher.hexdigest(),
                "size": count, **extra}
    except (OSError, TypeError, ValueError, RuntimeError, struct.error):
        _unsupported(label + " is unavailable or unsafe for identity capture.")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runtime_paths(root: Path) -> list[Path]:
    selected = []
    pending = [root]
    entries = 0
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path.name == "__pycache__":
                continue
            entries += 1
            if entries > MAX_RUNTIME_FILES:
                _unsupported("Controller runtime exceeds the identity inventory budget.")
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                _unsupported("Controller runtime identity cannot contain symbolic links.")
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif path.suffix == ".py" or path.is_relative_to(root / "resources"):
                if not stat.S_ISREG(mode):
                    _unsupported("Controller runtime identity requires regular source files.")
                selected.append(path)
    return sorted(selected)


def _runtime_identity() -> dict:
    root = Path(__file__).parent
    try:
        _check_path_components(root, "Controller runtime")
        paths = _runtime_paths(root)
        if not paths:
            _unsupported("Controller runtime source identity is empty.")
        before = {path: _stat_identity(path.lstat()) for path in paths}
        hasher = hashlib.sha256()
        size = 0
        for path in paths:
            item = _file_identity(path, maximum=MAX_RUNTIME_FILE_BYTES, label="Controller runtime source")
            size += item["size"]
            if size > MAX_RUNTIME_BYTES:
                _unsupported("Controller runtime exceeds the identity capture byte budget.")
            hasher.update(path.relative_to(root).as_posix().encode("utf-8") + b"\x00")
            hasher.update(item["sha256"].encode("ascii") + b"\x00")
        if (paths != _runtime_paths(root) or
                before != {path: _stat_identity(path.lstat()) for path in paths}):
            _unsupported("Controller runtime inventory changed during identity capture.")
        return {"sha256": "sha256:" + hasher.hexdigest(), "files": len(paths), "bytes": size,
                "python_version": platform.python_version()}
    except (OSError, UnicodeError, ValueError, RuntimeError):
        _unsupported("Controller runtime identity could not be captured.")


def _tool_identity(name: str, machine: str) -> dict:
    # This is the same fixed system-tool resolver used by the actual sandbox;
    # an executable selected through user PATH is never probed here.
    from .sandbox import _trusted_tool
    path = _trusted_tool(name)
    captured = _file_identity(path, maximum=MAX_NATIVE_BYTES, label="System sandbox tool", machine=machine)
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, timeout=4, check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, cwd="/")
            output.seek(0)
            raw = output.read(4097)
        version = raw.decode("utf-8").strip()
        if result.returncode != 0 or not version or len(raw) > 4096 or len(version) > 240 or any(ord(c) < 32 for c in version):
            _unsupported("System sandbox tool version could not be captured safely.")
        after = _file_identity(path, maximum=MAX_NATIVE_BYTES, label="System sandbox tool", machine=machine)
        if after != captured:
            _unsupported("System sandbox tool changed during its version probe.")
        return {**captured, "version": version}
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        _unsupported("System sandbox tool version could not be captured safely.")


def _machine_identity() -> dict:
    if platform.system() != "Linux" or platform.machine() not in _ELF_MACHINE:
        _unsupported("Native identity capture currently supports Linux x86_64 or aarch64 only.")
    descriptor = None
    try:
        descriptor = os.open("/proc/sys/kernel/random/boot_id", os.O_RDONLY | os.O_NOFOLLOW)
        boot_id = os.read(descriptor, 129).decode("ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id):
            _unsupported("Machine boot identity is unavailable.")
    except (OSError, UnicodeError):
        _unsupported("Machine boot identity is unavailable.")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {"system": "Linux", "release": platform.release(),
            "architecture": platform.machine(), "boot_id": boot_id}


def capture_native_identity(deployment: NativeDeployment) -> dict:
    """Capture controller-side identity; this does not grant native readiness.

    Re-capture and compare before launch. A changed executable, runtime, policy
    inventory, boot, kernel or sandbox tool invalidates a prior acceptance
    record. The selected host executable is hashed but never executed here.
    """
    if not isinstance(deployment, NativeDeployment):
        _invalid("Identity capture requires a validated native deployment descriptor.")
    # Revalidate even a manually constructed dataclass, without trusting _json.
    deployment = normalize_native_deployment(deployment.value)
    value = deployment.value
    if value["engine"] == "zcode":
        _unsupported("ZCode native identity requires a verified complete runtime distribution and policy scope; script-wrapper capture is not implemented.")
    policy = _system_policy_inventory(value["engine"])
    machine = _machine_identity()
    executable = _file_identity(Path(value["executable"]), maximum=MAX_NATIVE_BYTES,
        label="Native host executable", machine=machine["architecture"], require_static=True)
    runtime = _runtime_identity()
    sandbox_tools = {name: _tool_identity(name, machine["architecture"]) for name in ("bwrap", "unshare")}
    if _system_policy_inventory(value["engine"]) != policy:
        _unsupported("System policy changed during native identity capture.")
    result = {"schema_version": 1, "deployment_digest": deployment.digest,
              "engine": value["engine"], "protocol": value["protocol"],
              "executable": executable, "runtime": runtime, "machine": machine,
              "system_policy": policy, "sandbox_tools": sandbox_tools}
    return {**result, "identity_digest": digest(result)}


def _ancestor_policy_absence(root: Path):
    # Parent AGENTS instructions would otherwise disappear when the source is
    # copied to a private workspace. In-tree instructions remain in the source
    # baseline and are loaded normally; no instruction contents are read here.
    names = (".codex", ".agents", "hooks.json", "AGENTS.md", "AGENTS.override.md")
    for ancestor in root.parents:
        for name in names:
            try:
                (ancestor / name).lstat()
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RuntimeError):
                _unsupported("Ancestral project policy absence could not be established.")
            _unsupported("An ancestral native policy or instruction source would not be preserved by this standalone deployment.")


def _project_policy_tree(root: Path) -> tuple[int, dict]:
    """Walk by directory descriptors; never follow a source-tree symlink."""
    _check_path_components(root, "Project policy source")
    _ancestor_policy_absence(root)
    count = 0
    instruction_bytes = 0
    instruction_index_bytes = 0
    instructions = {}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(descriptor, depth, prefix=""):
        nonlocal count, instruction_bytes, instruction_index_bytes
        if depth > MAX_PROJECT_POLICY_DEPTH:
            _unsupported("Project policy inspection exceeds the directory-depth budget.")
        before = os.fstat(descriptor)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name == ".git":
                    continue
                count += 1
                if count > MAX_PROJECT_POLICY_ENTRIES:
                    _unsupported("Project policy inspection exceeds the entry-count budget.")
                name = entry.name.casefold()
                if name in (".codex", ".agents") or (depth == 0 and name == "hooks.json"):
                    _unsupported("The source contains native project policy that this standalone deployment cannot preserve.")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if name in ("agents.md", "agents.override.md"):
                        _unsupported("Project instruction aliases are not supported by this standalone deployment.")
                    # The executor separately rejects filesystem escapes. Here
                    # only policy discovery is performed, without following an
                    # arbitrary link into other projects or user configuration.
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if entry.name in ("AGENTS.md", "AGENTS.override.md"):
                        _unsupported("Project instruction sources must be regular files.")
                    child = os.open(entry.name, flags, dir_fd=descriptor)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            _unsupported("Project policy directories changed during inspection.")
                        walk(child, depth + 1, prefix + entry.name + "/")
                    finally:
                        os.close(child)
                elif entry.name in ("AGENTS.md", "AGENTS.override.md"):
                    relative = prefix + entry.name
                    encoded_path = relative.encode("utf-8")
                    instruction_index_bytes += len(encoded_path) + 128
                    if (len(encoded_path) > 4096 or len(instructions) >= MAX_PROJECT_INSTRUCTION_FILES or
                            instruction_index_bytes > MAX_INSTRUCTION_INDEX_BYTES):
                        _unsupported("Project instruction index exceeds the capture budget.")
                    contents = _regular_bytes(entry.name, maximum=MAX_INSTRUCTION_BYTES,
                        label="Project instruction file", dir_fd=descriptor,
                        expected_identity=_stat_identity(info))
                    instruction_bytes += len(contents)
                    if instruction_bytes > MAX_PROJECT_INSTRUCTION_BYTES:
                        _unsupported("Project instruction files exceed the aggregate capture budget.")
                    instructions[relative] = {
                        "sha256": "sha256:" + hashlib.sha256(contents).hexdigest(), "size": len(contents)}
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            _unsupported("Project policy directories changed during inspection.")

    descriptor = None
    try:
        descriptor = os.open(root, flags)
        identity = os.fstat(descriptor)
        walk(descriptor, 0)
        _check_path_components(root, "Project policy source")
        after = root.lstat()
        if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino):
            _unsupported("Project policy source was replaced during inspection.")
        _ancestor_policy_absence(root)
        return count, dict(sorted(instructions.items()))
    except (OSError, ValueError, RuntimeError):
        _unsupported("Project policy absence could not be established safely.")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def capture_project_policy(project_root: str | Path, *, workspace: str | Path | None = None) -> dict:
    """Require absent native config and preserved source instruction files.

    Call on actual enrolled and prepared directories, never a task-supplied
    assertion. The stable policy digest excludes scan statistics, ordinary code
    contents and the per-attempt workspace path. Native .codex and .agents
    sources, including ancestor skill/plugin trees, must be absent because
    their effective policy semantics are not transferred by this profile.
    Only exact in-tree AGENTS.md
    and AGENTS.override.md contents are read, bounded and hashed. Their relative
    paths and bytes must match any supplied prepared workspace exactly. An
    uncommitted, untracked or dependency-added instruction cannot disappear or
    alter the accepted instructions silently. Every supplied tree is checked
    afresh; its path and count are returned separately as inspection evidence.
    The prepared workspace remains independently bound by the executor's policy.
    """
    try:
        project_root = Path(project_root)
        _check_path_components(project_root, "Enrolled project")
        roots = [("source", project_root)]
        if workspace is not None:
            workspace = Path(workspace)
            _check_path_components(workspace, "Prepared workspace")
            roots.append(("workspace", workspace))
        checks = []
        instructions = None
        for role, root in roots:
            count, captured_instructions = _project_policy_tree(root)
            checks.append({"role": role, "root": str(root), "scanned_entries": count,
                           "instruction_count": len(captured_instructions)})
            if instructions is None:
                instructions = captured_instructions
            elif captured_instructions != instructions:
                raise Fault("NATIVE_PROJECT_POLICY_UNSUPPORTED",
                            "Prepared workspace instruction paths and bytes differ from the enrolled source policy.")
    except (OSError, TypeError, ValueError, RuntimeError):
        _unsupported("Project policy inputs are unavailable or unsafe for inspection.")
    binding = {"schema_version": 1, "project_root": str(project_root),
               "policy_profile": "standalone-project-v1",
               "instruction_digest": digest(instructions),
               "required_absence": ["native-project-config", "native-skills-and-plugins", "root-hooks", "ancestor-native-policy",
                                    "ancestor-instructions", "instruction-symlinks"]}
    return {**binding, "policy_digest": digest(binding), "instructions": instructions, "checks": checks}
