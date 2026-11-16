# Codex / ZCode 本地插件安装与验证

本页对应运行时 **0.5.0**。发行物是需要 **Python 3.11+、Git 和目标宿主 CLI** 的源码插件。它包含真实程序、英文 Skill、英文 AGENTS 受管块与宿主 Hook；不会静默改写全局配置、授予 Hook 信任或登录账号。

**当前验收边界：**协议、源码插件打包和注入器有自动化测试，Linux 沙箱和质量流水线有真实进程验证。真实 Codex `0.167.0-alpha.4` 已通过合成提供商的完整工具协议；ZCode 的固定源码选项映射也已实际执行。当前机器的管理政策阻断正式 Codex 独立 API 部署，完整 ZCode CLI 构建受到 npm 访问限制。真实提供商与已安装插件的完整 Hook/MCP 路径仍未验收。详见 [Codex](CODEX-COMPATIBILITY.en.md)、[ZCode](ZCODE-COMPATIBILITY.en.md) 和 [原生接入](NATIVE-HOST.zh-CN.md)。

## 1. 生成两个独立发行目录

在源码仓库根目录运行。目标目录应不存在或为空；已有非空目录会被拒绝覆盖。

```bash
python3 bin/parallel-dev package --adapter codex --output /absolute/path/to/dist/codex
python3 bin/parallel-dev package --adapter zcode --output /absolute/path/to/dist/zcode
python3 bin/parallel-dev package --adapter zcode --platform windows --output /absolute/path/to/dist/zcode-windows
```

等价 Python API：

```bash
PYTHONPATH=src python3 - <<'PY'
from pathlib import Path
from xiaolvji.installer import build_plugin
root = Path.cwd()
for adapter in ('codex', 'zcode'):
    print(build_plugin(adapter, root / 'dist' / adapter, root))
PY
```

构建 Windows ZCode 包时向 `build_plugin` 传入 `target_platform='windows'`，并使用另一个空目录。Windows 配置通过 `py -3` 执行 Python 文件；包内没有虚构的 `.exe`。构建可以跨平台进行，但不等于目标平台实测通过。

| 发行目录 | manifest | Hook 文件 | 程序 |
| --- | --- | --- | --- |
| Codex | `plugin.json` 与 `.codex-plugin/plugin.json` 兼容覆盖层 | `hooks/codex.json` | `bin/parallel-dev` |
| ZCode | `.zcode-plugin/plugin.json` | `hooks/hooks.json` | `bin/parallel-dev` |

Codex 包另带 portable `mcp.json`，兼容 manifest 指向同一个文件；ZCode 包另带自动发现的 `.mcp.json`。两包从同一源码复制 Skill 和策略；不要把两个发行目录直接合并。ZCode 自动发现 `hooks/hooks.json`，manifest 不再额外登记同一 Hook 来源，避免重复执行。每包附带 `SHA256SUMS` 与 `PACKAGE.json`，保留 `host_integration_tested:false`、`strict_execution_available:false`，并声明需要现场原生验收。源码布局本身不能携带目标机器的可用性证明。根 `INSTALL.zh-CN.md` 指向包内本页；指南、契约、需求、蓝图和设计文档按固定白名单复制，测试脚本和本地运行状态不装入插件。完整源码发行另含测试及发布工具。

### 初始化本机控制器

控制器状态目录放在开发项目和 Worker 副本之外；同一资源域的项目共用它，避免各开一个账本绕过总上限。

```bash
python3 bin/parallel-dev --state-dir /absolute/path/to/controller-state init --global-limit 4
python3 bin/parallel-dev doctor --engine codex
python3 bin/parallel-dev doctor --engine zcode
```

`doctor` 是状态查询；退出码 0 表示查询成功，不代表 `effective_mode` 可运行。必须读取它报告的模式和阻断原因。若使用非默认状态目录，让宿主进程继承 `PARALLEL_DEV_STATE_DIR=/absolute/path/to/controller-state`，使 Hook、MCP 和 CLI 指向同一个资源域。初始化只生成本地控制器角色凭据，不登录任何宿主。

严格模型任务需要目标机器的独立部署通过 `host verify`，并提供精确模型/质量快照；已有不支持迁移的策略会在凭据读取前阻断。配置与演示见 [提供商指南](PROVIDER-SETUP.zh-CN.md)。项目所有者原本明确选择可信同用户协作时，可显式注册以下模式；它不能作为严格任务失败后的自动退路：

```bash
python3 bin/parallel-dev --state-dir /absolute/path/to/controller-state project enable my-project /absolute/path/to/project --limit 4 --profile cooperative_native --acknowledge-cooperative
```

没有该确认参数，协作模式启用会被拒绝。批准计划、登记任务与运行队列的完整流程见根目录的运行时使用文档。

## 2. Codex 加载

以下配置用于**协调器的受信任宿主工作环境**。根据官方本地插件机制，可在该协调器环境内放置 `plugins/parallel-development`，并把生成的 Codex 包内容复制到该目录。将下面条目**合并**进该项目的 `.agents/plugins/marketplace.json`，保留原有插件条目；`source.path` 相对市场根目录。[官方插件文档](https://developers.openai.com/plugins/build/plugins)

```json
{
  "name": "xiaolvji-local",
  "plugins": [
    {
      "name": "parallel-development",
      "source": {"source": "local", "path": "./plugins/parallel-development"},
      "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
      "category": "Productivity"
    }
  ]
}
```

在支持本地插件的宿主中查看加载状态并完成其自身要求的信任审查。可信项目可以在 `.codex/config.toml` 中设置：

```toml
[plugins."parallel-development@xiaolvji-local"]
enabled = true
```

项目启用、Hook 信任、并行控制器项目注册是不同状态；仅复制文件不会自动授权运行。不要使用 `--dangerously-bypass-hook-trust` 验证安装。

**这个项目本地安装路径会引入 `.agents` 或 `.codex` 政策。** 首个严格独立 Worker 配置尚不能完整保留它们，因此不能把上面的项目内安装直接当作该项目 `host verify` 一定成功的流程。协调器的插件配置应放在被登记源码树及其祖先之外；受管项目自身仍必须满足策略要求。已经存在的政策不能删除或移走来绕过限制。协调器与 Worker 的权限、配置和验收分别处理。

协作 Worker 的基础命令为 `codex exec --json --sandbox workspace-write --cd ... -c agents.enabled=false -`，提示词从 stdin 输入。严格原生 Worker 则使用固定 App Server 适配器，在已获得证明的外部 Linux 沙箱内执行；必须先通过部署检查和 `host verify`。实际夹具已验证原生派发工具不出现在工具清单中，但这不等价于所有管理审批政策可迁移。模型、质量、策略和可执行文件均绑定验收记录；不会自动选择另一个模型或降低权限要求。[非交互接口](https://learn.chatgpt.com/docs/non-interactive-mode) · [配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

## 3. ZCode 加载

已核对 ZCode 源码提交 `a60ba6a7e5a06742270ca605f5d2cc2766afa7e1`。将发行目录的绝对路径合并进所选运行配置的 `plugins.dirs`，保留其他目录：

```json
{
  "plugins": {
    "enabled": true,
    "dirs": ["/absolute/path/to/dist/zcode"]
  }
}
```

使用宿主提供的检查命令确认结构和发现结果：

```bash
zcode plugins validate /absolute/path/to/dist/zcode --json
zcode plugins list --json
zcode skills list --json
```

这些命令来自已核对的 [ZCode 插件文档](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/10-plugins.md)。项目来源的 Hook 如被宿主要求信任，应先执行 review 并核对实际命令，再使用返回摘要手动授权；插件不代为授权：

```bash
zcode hooks trust review --workspace /absolute/path/to/project --json
zcode hooks trust grant --workspace /absolute/path/to/project --hook-digest REVIEWED_SHA256
zcode hooks trust status --workspace /absolute/path/to/project --json
```

声明变化后重新审查；不要复用旧摘要。[ZCode Hook 文档](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/09-hooks.md)

受管 Worker 显式设置 `--mode guarded`、`--workflow-mode disabled`，并禁用 Agent/Task/Workflow/CreateWorkflow/AmendWorkflow/ResumeWorkflowRun/SendMessage 原生工具，避免出现第二个队列。已核对接口的提示词通过 argv 传递，故包会检查长度并拒绝过长输入；argv 可被本机进程检查工具看到，任务描述不应携带凭证。没有使用未经核对的 `--prompt-file` 或自动 yolo 模式。

## 4. 英文 AGENTS 注入

注入器检查不会写入。运行时 CLI：

```bash
python3 bin/parallel-dev policy inspect --project /absolute/path/to/project
python3 bin/parallel-dev policy apply --project /absolute/path/to/project
python3 bin/parallel-dev policy remove --project /absolute/path/to/project
```

先查看 inspect 输出，再按意图执行 apply 或 remove；不要把上面三个示例当作必须依次运行的安装脚本。公共 Python API：

```bash
PYTHONPATH=src python3 - <<'PY'
from xiaolvji.installer import install_policy
print(install_policy('/absolute/path/to/project', 'inspect'))
# 审阅 inspect 结果后，由项目所有者选择 apply 或 remove：
# print(install_policy('/absolute/path/to/project', 'apply'))
# print(install_policy('/absolute/path/to/project', 'remove'))
PY
```

受管块之外的原字节、BOM 和换行符保留。已有 `AGENTS.override.md` 时不会偷偷创建低优先级 `AGENTS.md`；明确选择已有 override 才会注入。受管块被人工修改、所有权标记异常、备份损坏或存在未完成事务时，操作停止并保留现场。卸载只移除自己拥有的块。

## 5. 实际测试必须确认的事项

本地执行 `parallel-dev doctor` 后查看 `effective_mode`、缺失能力和证据等级。`documented` / `source_verified` / `detected` 都不等同 `integration_tested`。宿主测试至少应核对：正确的 Python 被找到、JSON stdout 能被宿主解析、未信任 Hook 确实被跳过并显示诊断、重复注册未发生、原生派发被阻断、已有权限决定未被无条件 allow 覆盖、取消后不重启、真实进程退出后账本才回收槽位。

Hook 只检查实际到达它的操作。Codex 的父会话 ID 不证明子代理身份；ZCode 普通 Agent 子代理在已审阅路径上不继承父 Hook runner；两端的 Stop、结果消息或异步启动响应都不用于释放容量。持续补位由运行中的控制器事件循环负责。Stop 提醒默认关闭，只有控制器已经原子预留续行预算时才允许返回一次有界续行请求，最多两次；等待人工审核不是续行理由。

## 6. 随包的本地 MCP 接口

两个宿主的 MCP 配置均启动包内 `bin/parallel-dev mcp`。Codex 使用已核对的 portable `mcp.json`，声明 `type: "stdio"` 并用 `${PLUGIN_ROOT}` 定位启动器；ZCode 使用 `.mcp.json` 和 `${ZCODE_PLUGIN_ROOT}`。POSIX 启动 Python 3，Windows 源码包启动 `py -3`。[Codex 本地 stdio 配置](https://developers.openai.com/plugins/deploy/desktop-only) · [ZCode MCP 配置解析源码](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/plugins/mcp.ts)

默认 MCP 身份为初始化时创建的 **reader**，提供 `parallel_status`、`parallel_events`、`parallel_queue`、`parallel_metrics`、`parallel_project_tree`、`parallel_read_file` 六个工具。队列和指标是有界只读诊断，最后两项只读取已登记且已有计划项目的有界已提交源码；不会启动任务、批准计划、验证交付或记录最终审核。包中没有身份文件或秘密值。需要通过 MCP 登记任务时，由可信协调端另外配置 `--identity-file /absolute/path/to/coordinator.key`，文件保持在 Worker 不可访问的控制器环境中；不要把 reviewer/operator 密钥放进插件目录、项目或任务提示词。

MCP 实现的是初始化握手型 **2025-11-25** 协议。支持新旧协议协商的客户端可回退；不能把本地协议测试解释为每个宿主版本已经兼容。主程序和生成包的真实 subprocess 测试已验证初始化、工具列表、源码读取及 reader 无权登记任务。Codex 原生隔离探针连接了 MCP 并报告原始工具清单，但没有完整走通安装后的模型工具名称与 Hook 调用；ZCode 前缀有固定源码依据。严格 Hook 只识别本插件已明确注册的名称，不用后缀或通配符放行。完整安装联调仍需目标宿主验证。
