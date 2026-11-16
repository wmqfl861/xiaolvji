"""Build host-specific source distributions and preserve owned AGENTS blocks."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile

from .contracts import Fault

EVENTS = {
    "codex": ("SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop", "Interrupt"),
    "zcode": ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"),
}

MAX_DOCUMENTATION_FILES = 1024
MAX_DOCUMENTATION_FILE_BYTES = 4 * 1024 * 1024
MAX_DOCUMENTATION_BYTES = 16 * 1024 * 1024
_CONTEXT_DOCUMENTS = (
    "README.md", "README.zh-CN.md", "REQUIREMENTS.zh-CN.md", "BLUEPRINT.zh-CN.md", "DESIGN.zh-CN.md",
    "COMPATIBILITY.zh-CN.md", "HANDOVER.zh-CN.md", "IMPLEMENTATION.zh-CN.md",
    "ACCEPTANCE.zh-CN.md", "DESIGN.html", "SOURCES.json", "VALIDATION.json", "reference/README.en.md",
)


def _resource(name: str) -> Path:
    packaged = Path(__file__).resolve().parent / "resources" / name
    if packaged.is_file():
        return packaged
    root = Path(__file__).resolve().parents[2]
    fallback = root / ("reference" if name == "inject_agents.py" else "spec") / name
    if not fallback.is_file():
        raise Fault("CAPABILITY_UNSUPPORTED", "The installed package is missing its owned injection resources.")
    return fallback


def install_policy(project: str | Path, action: str = "inspect", target: str = "AGENTS.md") -> dict:
    """Apply/remove exactly the managed block; do not change host trust/config."""
    if action not in ("inspect", "apply", "remove"):
        raise Fault("INVALID_INPUT", "Policy action must be inspect, apply, or remove.")
    script = _resource("inject_agents.py")
    spec = importlib.util.spec_from_file_location("_xiaolvji_owned_injector", script)
    if spec is None or spec.loader is None:
        raise Fault("CAPABILITY_UNSUPPORTED", "The injection module could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.Injector(project, target, _resource("AGENTS.block.en.md")).execute(action)
    except (module.InjectionError, OSError) as exc:
        raise Fault("INJECTION_BLOCKED", str(exc)) from exc


def _json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _hooks(adapter: str, target_platform: str) -> dict:
    hooks = {}
    for event in EVENTS[adapter]:
        if adapter == "codex":
            handler = {
                "type": "command",
                "command": 'python3 "${PLUGIN_ROOT}/bin/parallel-dev" hook --adapter codex',
                "commandWindows": 'py -3 "${PLUGIN_ROOT}/bin/parallel-dev" hook --adapter codex',
                "timeout": 3 if event == "Interrupt" else 10,
                "async": False,
            }
            if event in ("SessionStart", "SubagentStart"):
                handler["additionalContextLimit"] = 1500
        else:
            handler = {
                "type": "process", "command": "py" if target_platform == "windows" else "python3",
                "args": (["-3"] if target_platform == "windows" else []) + [
                    "${ZCODE_PLUGIN_ROOT}/bin/parallel-dev", "hook", "--adapter", "zcode"],
                "timeoutMs": 10000,
            }
            if event == "PreToolUse":
                handler["onError"] = "deny"
        hooks[event] = [{"hooks": [handler]}]
    return {"description": "Parallel Development runtime hooks; project opt-in and host trust are required.", "hooks": hooks}


def _check_tree(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_symlink():
            raise Fault("INVALID_INPUT", "Distribution sources must not contain symlinks.")


def _source_version(module_source: Path) -> str:
    """Read the version of the copied runtime without importing source code."""
    try:
        tree = ast.parse((module_source / "__init__.py").read_text(encoding="utf-8"))
        values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
                  and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise Fault("INVALID_INPUT", "The copied runtime version cannot be read.") from exc
    if len(values) != 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", values[0]):
        raise Fault("INVALID_INPUT", "The copied runtime needs one literal release version.")
    return values[0]


def _package_documentation(source: Path, stage: Path, version: str) -> None:
    """Keep guide-relative links intact without duplicating the source archive.

    Documentation and machine reports are bounded regular files. A symlink,
    special file, changing file, or oversized documentation tree fails the build
    instead of copying an unrelated file or waiting on a device/FIFO.
    """
    entries = 0
    total_bytes = 0

    def copy_file(path: Path):
        nonlocal total_bytes
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DOCUMENTATION_FILE_BYTES:
            raise Fault("INVALID_INPUT", "Package documentation must use bounded regular files.")
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_size,
                                     item.st_mtime_ns, item.st_ctime_ns)
            if identity(opened) != identity(before) or not stat.S_ISREG(opened.st_mode):
                raise Fault("INVALID_INPUT", "Package documentation changed while it was opened.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_DOCUMENTATION_FILE_BYTES + 1)
            if (len(data) != before.st_size or identity(os.fstat(descriptor)) != identity(before)
                    or identity(path.lstat()) != identity(before)):
                raise Fault("INVALID_INPUT", "Package documentation changed while it was copied.")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        total_bytes += len(data)
        if total_bytes > MAX_DOCUMENTATION_BYTES:
            raise Fault("INVALID_INPUT", "Package documentation exceeds its byte budget.")
        destination = stage / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def count_entry():
        nonlocal entries
        entries += 1
        if entries > MAX_DOCUMENTATION_FILES:
            raise Fault("INVALID_INPUT", "Package documentation exceeds its entry budget.")

    try:
        for name in ("docs", "schemas", "validation", "spec", "examples"):
            directory = source / name
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise Fault("INVALID_INPUT", "Package documentation directories must not use aliases.")
            pending = [(directory, 0)]
            while pending:
                parent, depth = pending.pop()
                if depth > 32:
                    raise Fault("INVALID_INPUT", "Package documentation exceeds its directory-depth budget.")
                with os.scandir(parent) as children:
                    for child in children:
                        count_entry()
                        if child.name.startswith("."):
                            continue  # Local/editor/account sidecars are not release documentation.
                        info = child.stat(follow_symlinks=False)
                        path = Path(child.path)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append((path, depth + 1))
                        elif stat.S_ISREG(info.st_mode):
                            copy_file(path)
                        else:
                            raise Fault("INVALID_INPUT", "Package documentation must not contain links or special files.")
        for name in _CONTEXT_DOCUMENTS:
            path = source / name
            if path.exists() or path.is_symlink():
                count_entry()
                copy_file(path)
    except OSError as exc:
        raise Fault("INVALID_INPUT", "Package documentation could not be copied safely.") from exc
    if not (stage / "docs" / "HOST-INSTALL.zh-CN.md").is_file():
        raise Fault("INVALID_INPUT", "The source snapshot is missing docs/HOST-INSTALL.zh-CN.md.")
    validation = ("\n包内 [VALIDATION.json](VALIDATION.json) 与 `validation/` 保留该源码快照的验证记录；"
                  "它们不替代目标机器的现场验收。\n" if (stage / "VALIDATION.json").is_file() else "")
    (stage / "INSTALL.zh-CN.md").write_text(
        f"# 小绿鸡 {version} 运行时插件包\n\n"
        "从 [宿主安装指南](docs/HOST-INSTALL.zh-CN.md) 开始。完整操作说明、配置契约和兼容性证据保留在 `docs/`，"
        "文档之间的相对链接保持原有目录结构。\n\n"
        "此包包括 Python 运行时、启动器、英文 Skill 与 AGENTS 注入策略、Hook、MCP 配置及使用文档。"
        "安装或解压不授予执行权限；受控模型执行仍需目标机器的能力检查、明确配置和 `host verify` 现场验收。\n\n"
        "此包用于运行和宿主加载。继续开发、重跑完整测试或重新构建发行包，应使用同一版本的完整源码交付包，"
        "或 [项目仓库](https://github.com/wmqfl861/xiaolvji) 中对应源码快照；测试与开发脚本未重复装入本运行时包。\n"
        + validation, encoding="utf-8")


def build_plugin(adapter: str, destination: str | Path, source_root: str | Path,
                 target_platform: str | None = None) -> dict:
    """Build a fresh Python 3.11+ package, not an installed or trusted host plugin."""
    if adapter not in EVENTS:
        raise Fault("CAPABILITY_UNSUPPORTED", "Unknown plugin adapter.")
    target_platform = target_platform or ("windows" if os.name == "nt" else "posix")
    if target_platform not in ("posix", "windows"):
        raise Fault("INVALID_INPUT", "Target platform must be posix or windows.")
    source = Path(source_root).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise Fault("INVALID_INPUT", "Distribution destination must not pass through symlinks.")
    module_source = source / "src" / "xiaolvji"
    if not module_source.is_dir():
        raise Fault("INVALID_INPUT", "Source root does not contain the runtime package.")
    if destination == source or destination.is_relative_to(module_source) or source.is_relative_to(destination):
        raise Fault("INVALID_INPUT", "Distribution destination overlaps its runtime sources.")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise Fault("DESTINATION_EXISTS", "Refusing to replace a nonempty distribution destination.")
    _check_tree(module_source)
    version = _source_version(module_source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".xiaolvji-build-", dir=destination.parent))
    try:
        shutil.copytree(module_source, stage / "src" / "xiaolvji", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        resource_dir = stage / "src" / "xiaolvji" / "resources"
        resource_dir.mkdir(parents=True, exist_ok=True)
        for name, folder in (("AGENTS.block.en.md", "spec"), ("parallel-development.SKILL.md", "spec"), ("inject_agents.py", "reference")):
            original = source / folder / name
            if original.is_symlink() or not original.is_file():
                raise Fault("INVALID_INPUT", "Canonical policy or injection source is missing.")
            shutil.copyfile(original, resource_dir / name)
        (stage / "policies").mkdir()
        shutil.copyfile(resource_dir / "AGENTS.block.en.md", stage / "policies" / "AGENTS.block.en.md")
        skills = stage / "skills" / "parallel-development"
        skills.mkdir(parents=True)
        shutil.copyfile(resource_dir / "parallel-development.SKILL.md", skills / "SKILL.md")
        (stage / "bin").mkdir()
        launcher = source / "bin" / "parallel-dev"
        if launcher.is_file() and not launcher.is_symlink():
            shutil.copyfile(launcher, stage / "bin" / "parallel-dev")
        else:
            (stage / "bin" / "parallel-dev").write_text(
                '#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))\nfrom xiaolvji.cli import main\nraise SystemExit(main())\n', encoding="utf-8")
        (stage / "bin" / "parallel-dev").chmod(0o755)
        manifest = {"name": "parallel-development", "version": version,
                    "description": "Bounded parallel development with explicit contracts, admission accounting, and independent quality gates."}
        plugin_root = "${PLUGIN_ROOT}" if adapter == "codex" else "${ZCODE_PLUGIN_ROOT}"
        mcp_server = {"type": "stdio", "command": "py" if target_platform == "windows" else "python3",
                      "args": (["-3"] if target_platform == "windows" else []) + [plugin_root + "/bin/parallel-dev", "mcp"],
                      "cwd": plugin_root}
        if adapter == "codex":
            _json_file(stage / "mcp.json", {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                                          "mcpServers": {"parallel-development": mcp_server}})
            _json_file(stage / "plugin.json", {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", **manifest,
                                              "extensions": {"com.openai": {"hooks": "./hooks/codex.json", "requires_local_executor": True}}})
            _json_file(stage / ".codex-plugin" / "plugin.json", {**manifest, "skills": "./skills/", "hooks": "./hooks/codex.json", "mcpServers": "./mcp.json", "requires_local_executor": True})
            _json_file(stage / "hooks" / "codex.json", _hooks(adapter, target_platform))
        else:
            # ZCode automatically discovers hooks/hooks.json; do not also add an
            # explicit manifest hook source, which would register it twice.
            _json_file(stage / ".mcp.json", {"mcpServers": {"parallel-development": mcp_server}})
            _json_file(stage / ".zcode-plugin" / "plugin.json", manifest)
            _json_file(stage / "hooks" / "hooks.json", _hooks(adapter, target_platform))
        _json_file(stage / "PACKAGE.json", {
            "name": "xiaolvji-parallel-development", "version": version, "adapter": adapter,
            "target_platform": target_platform, "python_requires": ">=3.11",
            "distribution": "python-source", "host_integration_tested": False,
            "strict_execution_available": False,
            "execution_profiles": ["cooperative_native", "controlled_executor"],
            "controlled_executor_platforms": ["linux"],
            "supported_native_acceptance_profiles": (["codex-app-server-v2"] if adapter == "codex" else []),
            "requires_local_host_acceptance": True,
            "capability_claim_scope": "Portable package metadata does not establish installation, native integration, or execution capability on this machine.",
            "permissions_granted": False,
            "mcp_default_role": "reader", "mcp_protocol": "2025-11-25",
            "documentation": "INSTALL.zh-CN.md",
        })
        _package_documentation(source, stage, version)
        hashes = []
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                hashes.append(hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.relative_to(stage).as_posix())
        (stage / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")
        if destination.exists():
            destination.rmdir()  # refuses a racing nonempty directory
        stage.replace(destination)
        return {"status": "built", "adapter": adapter, "path": str(destination),
                "target_platform": target_platform, "files": len(hashes) + 1,
                "installed": False, "host_integration_tested": False, "requires_local_host_acceptance": True,
                "python_requires": ">=3.11"}
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
