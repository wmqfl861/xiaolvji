#!/usr/bin/env python3
"""Build and verify an offline delivery from one immutable Git commit.

Usage: python3 scripts/build_release.py --output /absolute/new-delivery.zip
Only tracked commit files and freshly generated artifacts enter the archive.
The working tree may be dirty; its uncommitted contents are never copied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

MAX_TOOL_OUTPUT = 32 * 1024 * 1024
PROHIBITED_NAMES = {
    ".git", ".parallel-dev", "__pycache__", ".venv", "node_modules",
    "authority.json", "ledger.sqlite3", "credentials.json", "auth.json",
    ".env", ".netrc", "id_rsa", "id_ed25519", "id_ecdsa", "github-auth-runtime",
}


class BuildError(Exception):
    pass


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def recorded_execution_status(source: Path, version: str) -> tuple[dict, dict]:
    """Read claims from the extracted commit, never from this host or worktree.

    Packaging verifies the evidence file's provenance, not its runtime claims.
    Missing evidence stays unknown; a report for another version is rejected.
    """
    path = source / "VALIDATION.json"
    evidence = {"path": "source/VALIDATION.json", "available": False,
                "runtime_tests_rerun_by_builder": False}
    capabilities = {}
    if path.is_file():
        if path.is_symlink() or path.stat().st_size > 1_048_576:
            raise BuildError("Selected source validation must be a bounded regular file.")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BuildError("Selected source validation contains duplicate JSON keys.")
                result[key] = value
            return result

        def invalid_constant(value):
            raise BuildError("Selected source validation contains a non-finite JSON number.")

        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        if not isinstance(value, dict) or value.get("runtime_version") != version:
            raise BuildError("Selected source validation does not bind the selected package version.")
        capabilities = value.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise BuildError("Selected source validation capabilities must be an object.")
        evidence.update(available=True, sha256=sha256_file(path), runtime_version=version)
        for key in ("schema_version", "date", "status"):
            if key in value:
                evidence[key] = value[key]

    def status(key):
        value = capabilities.get(key)
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) else "not_asserted"

    def reported(key):
        return "reported_in_selected_validation" if capabilities.get(key) is True else "not_asserted"

    execution = {
        "linux_cooperative_runtime": status("linux_cooperative_process_gateway"),
        "controlled_executor": status("controlled_executor"),
        "model_transport": status("model_transport"),
        "codex_cli_loading": status("codex_cli_loading"),
        "codex_native_protocol": status("codex_native_protocol"),
        "zcode_source_mapping": status("zcode_source_mapping"),
        "native_production_current_host": status("native_production_current_host"),
        "real_codex_zcode_host_tests": reported("real_host_integration"),
        "windows_execution_tests": reported("windows_execution"),
        "macos_execution_tests": reported("macos_execution"),
        "windows_macos_execution_tests": "reported_in_selected_validation"
            if capabilities.get("windows_execution") is True and capabilities.get("macos_execution") is True
            else "not_asserted",
        "formal_release_approval": "not_asserted",
        "remote_publication": "not_performed_or_verified_by_this_build",
    }
    return execution, evidence


def execution_summary_zh(execution: dict, evidence: dict) -> str:
    """Describe only the chosen snapshot's recorded capability scope."""
    paragraphs = [
        "以下状态来自所选提交的 `source/VALIDATION.json`；其摘要记录在 `DELIVERY.json`。构建过程不重新运行这些测试。"
        if evidence["available"] else
        "所选提交没有 `VALIDATION.json`；本构建不声明其运行能力。"
    ]
    strict = execution["controlled_executor"]
    if strict == "linux_offline_integration_tested":
        paragraphs.append("该提交记录了 Linux 严格离线命令与检查的集成验证；目标机器仍须通过现场能力探测。")
    elif strict in {"blocked_not_implemented", "blocked_no_verified_os_backend"}:
        paragraphs.append("该提交尚无已验证的严格 OS 隔离后端，`controlled_executor` 保持阻断。")
    transport = execution["model_transport"]
    if transport == "synthetic_transport_integration_tested_native_hosts_blocked":
        paragraphs.append("该提交记录了受限 IPC 与私有 loopback 提供商通道的合成测试；默认执行仍离线，只有显式绑定通道的路径启用该能力。合成提供商不等于真实宿主模型。")
    elif transport == "native_codex_protocol_fixture_verified_managed_deployment_blocked":
        paragraphs.append("该提交记录了真实 Codex 二进制通过受控执行器、私有通道及合成服务完成工具轮次；没有调用真实外部模型。正式执行要求按部署进行现场验收，当前机器因管理策略无法完整保留而阻断。所有质量检查仍离线。")
    elif transport.startswith("blocked_"):
        paragraphs.append("真实模型任务所需的受限提供商通道仍被阻断；离线 OS 验证不代表 Codex/ZCode 模型任务已经可用。")
    if execution["real_codex_zcode_host_tests"] != "reported_in_selected_validation":
        paragraphs.append("所选记录没有确认真实外部提供商、安装后的完整 Hook/MCP 路径及完整 ZCode CLI 验收。原生协议夹具成功不会代替这些部署事实，也不授予目标机器运行权限。")
    if execution["codex_cli_loading"] == "configuration_tested_protocol_blocked":
        paragraphs.append("Codex 本地 CLI 已有配置加载证据，但完整协议验证被阻断；这不证明真实模型轮次或 Hook 加载成功。")
    if execution["zcode_source_mapping"] == "pinned_native_option_compiler_executed":
        paragraphs.append("ZCode 的完整固定源码已重建，原生选项编译器及相关纯函数已实际执行；完整 CLI 构建受到 npm 访问限制，不能把源码映射等同完整宿主可运行。")
    rows = [
        ("Linux 协作执行", "linux_cooperative_runtime"),
        ("严格离线执行", "controlled_executor"),
        ("受限模型通道", "model_transport"),
        ("Codex CLI 局部检查", "codex_cli_loading"),
        ("Codex 原生合成协议", "codex_native_protocol"),
        ("ZCode 原生源码映射", "zcode_source_mapping"),
        ("当前机器正式原生部署", "native_production_current_host"),
        ("真实 Codex/ZCode 宿主联调", "real_codex_zcode_host_tests"),
        ("Windows 执行", "windows_execution_tests"),
        ("macOS 执行", "macos_execution_tests"),
    ]
    paragraphs.append("| 能力 | 所选提交记录的状态 |\n| --- | --- |\n" +
                      "\n".join(f"| {label} | `{execution[key]}` |" for label, key in rows))
    paragraphs.append("`not_asserted` 表示本包没有声明该项验证成功。不得用包摘要校验代替运行能力验收；本构建不授予权限、不改变信任，也不声明正式发布审批或远端发布成功。")
    return "\n\n".join(paragraphs)


def run(argv: list[str], *, cwd: Path | None = None, timeout: int = 180) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1"})
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError("Local build command failed: " + type(exc).__name__) from exc
        if completed.returncode:
            err.seek(0)
            diagnostic = err.read(2000).decode("utf-8", "replace").strip()
            raise BuildError(f"Local build command exited {completed.returncode}: {diagnostic}")
        out.seek(0)
        result = out.read(MAX_TOOL_OUTPUT + 1)
        if len(result) > MAX_TOOL_OUTPUT:
            raise BuildError("Local build command output exceeded its limit.")
        return result


def git(repository: Path, *args: str, timeout: int = 180) -> bytes:
    return run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
                "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.ext.allow=never",
                "-c", "protocol.file.allow=always", "-C", str(repository), *args], timeout=timeout)


def safe_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name or ":" in name:
        raise BuildError("Unsafe artifact member name.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in name.split("/")):
        raise BuildError("Artifact member leaves its distribution root.")
    for part in path.parts:
        lowered = part.casefold()
        if (lowered in PROHIBITED_NAMES or lowered.endswith((".key", ".sqlite", ".sqlite3", ".db", ".pyc"))
                or lowered.startswith(("github-auth-runtime-", "github-auth-", "auth-runtime-"))):
            raise BuildError("Private/runtime state or dependency cache is tracked; release refused: " + name)
    return path


def tree_entries(repository: Path, revision: str, *, allow_historical_links: bool = False) -> dict[str, dict]:
    result = {}
    folded = set()
    for record in git(repository, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = header.decode("ascii").split()
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildError("Release source paths must be UTF-8.") from exc
        safe_name(name)
        if name.casefold() in folded:
            raise BuildError("Release source paths collide on a case-insensitive platform: " + name)
        folded.add(name.casefold())
        if kind != "blob" or mode not in {"100644", "100755"}:
            if not (allow_historical_links and kind == "blob" and mode == "120000"):
                raise BuildError("Release source must contain regular files only; submodules and links are unsupported: " + name)
        result[name] = {"mode": mode, "object_id": object_id}
    return result


def extract_source(repository: Path, commit: str, destination: Path, archive: Path, entries: dict) -> None:
    git(repository, "archive", "--format=tar", "--output=" + str(archive), commit)
    destination.mkdir(mode=0o700)
    seen = set()
    with tarfile.open(archive, "r:") as source:
        for member in source:
            name = member.name.rstrip("/") if member.isdir() else member.name
            relative = safe_name(name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or name not in entries or name in seen:
                raise BuildError("Git archive contains an unexpected, duplicate or non-regular member.")
            seen.add(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            reader = source.extractfile(member)
            if reader is None:
                raise BuildError("Cannot read a regular Git archive member.")
            with reader, target.open("xb") as output:
                shutil.copyfileobj(reader, output, length=1024 * 1024)
            target.chmod(0o755 if entries[name]["mode"] == "100755" else 0o644)
    if seen != set(entries):
        raise BuildError("Git archive omitted tracked files (for example export-ignore); refusing an incomplete source delivery.")
    verify_source(destination, entries)


def inventory(root: Path) -> dict[str, Path]:
    result = {}
    folded = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BuildError("Generated distribution contains a symlink.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BuildError("Generated distribution contains a non-regular file.")
        name = path.relative_to(root).as_posix()
        safe_name(name)
        if name.casefold() in folded:
            raise BuildError("Generated distribution paths collide on a case-insensitive platform.")
        folded.add(name.casefold())
        result[name] = path
    return result


def verify_source(source: Path, entries: dict) -> None:
    actual = inventory(source)
    if set(actual) != set(entries):
        raise BuildError("The source snapshot does not contain exactly the selected tracked files.")
    for name, entry in entries.items():
        path = actual[name]
        oid = hashlib.sha1() if len(entry["object_id"]) == 40 else hashlib.sha256()
        oid.update(b"blob " + str(path.stat().st_size).encode("ascii") + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                oid.update(chunk)
        if oid.hexdigest() != entry["object_id"]:
            raise BuildError("Source bytes differ from Git (possibly export-subst): " + name)
        if os.name == "posix" and bool(path.stat().st_mode & 0o111) != (entry["mode"] == "100755"):
            raise BuildError("Source executable mode differs from the selected commit: " + name)


def verify_checksums(root: Path) -> int:
    files = inventory(root)
    manifest = files.get("SHA256SUMS")
    if manifest is None:
        raise BuildError("Distribution lacks SHA256SUMS.")
    recorded = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            expected, name = line.split("  ", 1)
        except ValueError as exc:
            raise BuildError("Malformed checksum entry.") from exc
        safe_name(name)
        if not re.fullmatch(r"[a-f0-9]{64}", expected) or name in recorded or name == "SHA256SUMS":
            raise BuildError("Invalid or duplicate checksum entry.")
        recorded[name] = expected
    if set(recorded) != set(files) - {"SHA256SUMS"}:
        raise BuildError("Checksum manifest does not cover exactly the payload.")
    for name, expected in recorded.items():
        if sha256_file(files[name]) != expected:
            raise BuildError("Checksum mismatch: " + name)
    return len(recorded)


def write_checksums(root: Path) -> int:
    files = inventory(root)
    if "SHA256SUMS" in files:
        raise BuildError("Refusing to replace an existing delivery checksum manifest.")
    lines = [sha256_file(path) + "  " + name for name, path in sorted(files.items())]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return verify_checksums(root)


def zip_tree(root: Path, destination: Path, epoch: int) -> dict:
    files = inventory(root)
    timestamp = datetime.fromtimestamp(max(315532800, min(epoch, 4354819198)), timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, timestamp)
            info.create_system = 3
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise BuildError("ZIP CRC validation failed.")
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(files):
            raise BuildError("ZIP member inventory does not match the staged payload.")
        for member in archive.infolist():
            safe_name(member.filename)
            mode = (member.external_attr >> 16) & 0o170000
            if mode != stat.S_IFREG:
                raise BuildError("ZIP contains a non-regular member.")
            hasher = hashlib.sha256()
            with archive.open(member) as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
            if hasher.hexdigest() != sha256_file(files[member.filename]):
                raise BuildError("ZIP member hash does not match the staged file.")
    return {"files": len(files), "bytes": destination.stat().st_size, "sha256": sha256_file(destination)}


def build_plugins(source: Path, work: Path, output: Path, version: str, epoch: int) -> list[dict]:
    output.mkdir()
    results = []
    script = (
        "import json,sys; from pathlib import Path; "
        "sys.path.insert(0,str(Path(sys.argv[1])/'src')); "
        "from xiaolvji.installer import build_plugin; "
        "print(json.dumps(build_plugin(sys.argv[2],sys.argv[3],sys.argv[1],target_platform=sys.argv[4])))"
    )
    for adapter in ("codex", "zcode"):
        for target in ("posix", "windows"):
            directory = work / (adapter + "-" + target)
            raw = run([sys.executable, "-I", "-B", "-c", script, str(source), adapter, str(directory), target])
            result = json.loads(raw)
            if result.get("status") != "built" or result.get("installed") is not False:
                raise BuildError("Committed installer did not return a source-package build result.")
            count = verify_checksums(directory)
            name = f"parallel-development-{adapter}-{target}-{version}.zip"
            packed = zip_tree(directory, output / name, epoch)
            results.append({"adapter": adapter, "platform": target, "path": "plugins/" + name,
                            **packed, "checksum_entries": count, "native_host_tested": False})
    return results


def create_bundle(repository: Path, commit: str, work: Path, target: Path, branch_ref: str) -> dict:
    if git(repository, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise BuildError("A full-history delivery cannot be built from a shallow repository.")
    # Historical filenames are checked too: a bundle includes deleted history.
    revisions = git(repository, "rev-list", commit).decode("ascii").splitlines()
    for revision in revisions:
        tree_entries(repository, revision, allow_historical_links=True)
    object_format = git(repository, "rev-parse", "--show-object-format").decode("ascii").strip()
    export = work / "bundle-export.git"
    run(["git", "init", "--bare", "--object-format=" + object_format, str(export)])
    git(export, "symbolic-ref", "HEAD", branch_ref)
    git(export, "fetch", "--no-tags", "--no-write-fetch-head", "--", str(repository), commit + ":" + branch_ref)
    if git(export, "rev-parse", "HEAD").decode("ascii").strip() != commit:
        raise BuildError("Temporary bundle export did not resolve the chosen commit.")
    target.parent.mkdir(parents=True)
    git(export, "bundle", "create", str(target), "HEAD", branch_ref)
    git(export, "bundle", "verify", str(target))
    heads = {}
    for line in git(export, "bundle", "list-heads", str(target)).decode("utf-8").splitlines():
        oid, name = line.split(" ", 1)
        heads[name] = oid
    if heads.get("HEAD") != commit or heads.get(branch_ref) != commit or set(heads.values()) != {commit}:
        raise BuildError("Bundle heads differ from the selected commit/ref.")
    with target.open("rb") as bundle:
        first = bundle.readline()
        if first not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
            raise BuildError("Unknown Git bundle header.")
        for _ in range(10000):
            line = bundle.readline()
            if line == b"\n":
                break
            if not line or line.startswith(b"-"):
                raise BuildError("Bundle is incomplete or has history prerequisites.")
        else:
            raise BuildError("Bundle header exceeds its size limit.")
    if git(export, "rev-list", commit).decode("ascii").splitlines() != revisions:
        raise BuildError("Bundle export does not contain the complete chosen history.")
    return {"path": "git/" + target.name, "commit": commit, "branch_ref": branch_ref,
            "history_commits": len(revisions), "complete_history": True,
            "bytes": target.stat().st_size, "sha256": sha256_file(target)}


def build(repository: Path, revision: str, destination: Path) -> dict:
    if not destination.is_absolute() or ".." in destination.parts:
        raise BuildError("--output must be an absolute path without traversal.")
    if destination.exists() or destination.is_symlink():
        raise BuildError("Output already exists; refusing to replace it.")
    for parent in destination.parents:
        if parent.is_symlink():
            raise BuildError("Output directory must not pass through symlinks.")
    repository = repository.resolve(strict=True)
    root = Path(git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if destination.is_relative_to(root):
        raise BuildError("The delivery must be written outside the source repository.")
    commit = git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise BuildError("Git did not resolve an immutable commit.")
    entries = tree_entries(root, commit)
    if "src/xiaolvji/installer.py" not in entries or "pyproject.toml" not in entries:
        raise BuildError("Selected commit does not contain the runtime and source-package installer.")
    head = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    refs = git(root, "for-each-ref", "--points-at=" + commit, "--format=%(refname)", "refs/heads/").decode().splitlines()
    current = git(root, "branch", "--show-current").decode().strip()
    branch_ref = "refs/heads/" + current if current and head == commit else (sorted(refs)[0] if refs else "refs/heads/release-snapshot")
    epoch = int(git(root, "show", "-s", "--format=%ct", commit).decode().strip())
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=normal").strip())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xiaolvji-release-", dir=destination.parent) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        payload.mkdir()
        source = payload / "source"
        extract_source(root, commit, source, work / "source.tar", entries)
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        version = metadata["project"]["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?", version):
            raise BuildError("Package version is not a safe release version.")
        execution, validation_evidence = recorded_execution_status(source, version)
        execution_summary = execution_summary_zh(execution, validation_evidence)
        plugins = build_plugins(source, work, payload / "plugins", version, epoch)
        short = git(root, "rev-parse", "--short=12", commit).decode("ascii").strip()
        bundle = create_bundle(root, commit, work, payload / "git" / ("runtime-" + short + ".bundle"), branch_ref)
        branch = branch_ref.removeprefix("refs/heads/")
        start = f'''# xiaolvji {version} 完整交付包

源码提交：`{commit}`。本包从该提交的 Git 对象生成；未提交文件不在交付中。

## 从哪里开始

- `source/docs/RUNTIME.zh-CN.md`：实际运行指南。
- `source/docs/LIMITATIONS.zh-CN.md`：当前能力与限制。
- `source/docs/HOST-INSTALL.zh-CN.md`：Codex / ZCode 本地加载方法。
- `source/`：该提交的全部受支持跟踪文件，包括源代码、测试和文档。
- `plugins/`：Codex / ZCode × POSIX / Windows 的四个独立 Python 源码插件 ZIP。
- `{bundle['path']}`：所选提交和命名分支的完整 Git 历史。
- `DELIVERY.json`：精确提交、数量、组件摘要及构建验证信息。
- `SHA256SUMS`：除清单自身外的全部交付文件摘要。

## 当前状态

{execution_summary}

Windows ZIP 是 Python 加载配置，不含原生可执行文件。选择执行模式和宿主时，以该提交的运行文档及现场能力探测为准，不自动降级。

最终审核需要指定独立 reviewer 对精确交付摘要作真实决定。本次打包没有授予批准，没有执行或验证远端推送，也不表示已经公开发布。

## 查看或运行源码

解压后，可直接执行：

```bash
python3 source/bin/parallel-dev --help
python3 source/bin/parallel-dev doctor
```

Python 要求 3.11+；实际任务还要求 Git、受支持的 Linux 进程能力，以及对应宿主 CLI。`doctor` 的查询成功不代表严格模式可运行，应读取 `effective_mode` 和阻断原因。

需要继续 Git 开发时，从完整 bundle 恢复：

```bash
git clone --branch {shlex.quote(branch)} {shlex.quote(bundle['path'])} xiaolvji-runtime
```

在支持该命令的系统中，可校验外层全部文件：

```bash
sha256sum -c SHA256SUMS
```

四个插件 ZIP 内也各有摘要清单。安装前选择一个宿主/平台包，按宿主自己的本地插件加载和信任流程处理；不要将四个插件目录混合或把审核凭据放进插件目录。
'''
        (payload / "START-HERE.zh-CN.md").write_text(start, encoding="utf-8")
        # source + 4 plugin ZIPs + bundle + START-HERE + DELIVERY + SHA256SUMS.
        file_count = len(entries) + len(plugins) + 4
        delivery = {
            "schema_version": 1, "name": "xiaolvji", "version": version,
            "source_commit": commit, "source_ref": branch_ref, "source_commit_time_utc": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
            "source_files": len(entries), "payload_files": file_count,
            "working_tree_changes_excluded": dirty,
            "plugins": plugins, "git_bundle": bundle,
            "validation": {"source_matches_git_objects": True, "plugin_member_hashes_and_crc_verified": True,
                           "plugin_checksum_manifests_verified": True, "bundle_complete_history_verified": True},
            "source_validation": validation_evidence,
            "execution_status": execution,
        }
        (payload / "DELIVERY.json").write_text(json.dumps(delivery, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        verify_source(source, entries)  # Generated package imports must not mutate source.
        checksums = write_checksums(payload)
        if checksums + 1 != file_count:
            raise BuildError("Delivery file count is inconsistent.")
        packed = zip_tree(payload, work / "delivery.zip", epoch)
        if packed["files"] != file_count:
            raise BuildError("Delivery ZIP inventory is inconsistent.")
        # Same-filesystem hard-link publish is atomic and refuses existing paths.
        os.link(work / "delivery.zip", destination)
        return {"status": "built_and_verified", "path": str(destination), "source_commit": commit,
                "source_ref": branch_ref, "source_files": len(entries), "plugin_packages": len(plugins),
                "payload_files": packed["files"], "bytes": packed["bytes"], "sha256": packed["sha256"],
                "working_tree_changes_excluded": dirty, "archive_crc_and_member_hashes_verified": True,
                "network_used": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1], help="Source Git repository; defaults to this script's repository")
    arguments = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise BuildError("Python 3.11 or newer is required.")
        result = build(arguments.repo, arguments.commit, arguments.output)
        print(canonical_json(result))
        return 0
    except (BuildError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(canonical_json({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
