# 原生宿主接入与持续运行

0.6.0 提供两个独立入口。`host inspect-plugin` 核对已安装的 Codex 协调器，`host verify` 验收严格 Worker 的目标部署。协调器的检查结果不授予 Worker 执行权限。

`host verify` 使用真实 Codex 二进制和固定本地 HTTP/SSE 夹具，检查工具执行、写入范围、网络隔离和完整终态；它不读取真实 API 密钥，也不声称真实提供商已经通过。

当前开发环境的结果必须分开理解：独立合成配置下的 Codex 原生工具轮次通过；保留本机 `/etc/codex` 管理要求后，支持范围检查正确阻断，提供商请求为零。完整 ZCode CLI 尚不能构建，原因是公开 npm 域名不可达；其固定源码中的模型选项编译器已实际执行验证。完整记录见 [Codex](CODEX-COMPATIBILITY.en.md)、[ZCode](ZCODE-COMPATIBILITY.en.md) 和 [VALIDATION.json](../VALIDATION.json)。

## 1. 首个支持的原生部署范围

当前提供 `codex-app-server-v2` 独立 API 部署路径。它要求 Linux、受控执行器、静态 Codex ELF 二进制、明确的模型与质量配置，以及不存在需要迁移的系统或项目宿主政策。

这里的“独立”是部署者明确选择新的执行上下文，不是复制现有 ChatGPT/Codex 账号、企业策略或用户 HOME。该路径不会自动继承现有账户中的审批、规则、Hook、MCP 和插件。不能把它用于规避原来必须适用的策略。

以下情况会阻断：

- `/etc/codex` 存在，包括符号链接或无法检查的情况。当前没有完整的管理策略迁移实现，也没有忽略开关。
- 源项目或实际工作副本内存在 `.codex`、`.agents` 技能/插件目录、根级 `hooks.json`，或祖先目录包含会丢失的宿主政策/AGENTS 指令。
- 源项目中的 AGENTS 指令与实际 Git 工作副本不一致，包括未提交或未跟踪的指令；指令文件落在 Worker 可写范围内也会阻断。
- 模型或推理级别在宿主实际模型目录中不存在；显式质量字段没有被实际加载；请求未满足代理中的精确约束。
- 二进制、运行时代码、系统工具、系统启动身份、模型配置或指令发生变化，使旧验收失效。

ZCode 可以解析部署描述，但完整发行依赖和策略范围尚未验证，原生身份捕获会明确拒绝。Windows/macOS、动态链接宿主和任意脚本启动器不属于首个已实现的原生发行闭包。

## 2. 为什么使用 App Server

普通 `codex exec --sandbox workspace-write` 可以在本机受控边界内完成文本轮次，但其工具执行会再次创建沙箱，受到嵌套命名空间限制。Codex 官方 App Server 支持已经由外部隔离的服务器使用 `externalSandbox`。本版只在已经获得 supervisor 隔离证明的任务里使用这条路径，实际权限仍由外层只读挂载、范围可写挂载、seccomp、私有网络和受限提供商代理决定。[官方接口说明](https://learn.chatgpt.com/docs/app-server)

外部模式不能被解释为保留了普通原生沙箱的全部审批语义。实际探针观察到，外部模式中的提权参数或规则提示不一定产生原生审批请求；命令仍受到外层原有权限限制，没有获得额外权限。因此，有现成管理政策的部署继续阻断，不能仅凭文件复制或 `configRequirements/read` 的部分输出宣布迁移成功。

适配器不接受原生审批、交互输入、凭据刷新或工具授权请求。它会拒绝或终止这类请求，不把无人值守解释为自动批准。已完成回合的常驻 App Server 会被有界关闭；必要的服务终止信号和真实退出码写入日志。候选生成仍等待外层进程树退出证明。

## 3. 登记并验收目标机器

先初始化并启用严格项目，登记经过批准的计划。状态目录必须在源码项目和工作副本之外；所有项目使用同一资源域。

```bash
parallel-dev --state-dir /absolute/controller-state init --global-limit 4
parallel-dev --state-dir /absolute/controller-state project enable my-project /absolute/source --limit 4 --profile controlled_executor
```

保存明确的部署文件，例如 `/absolute/codex-deployment.json`。`executable` 必须指向实际二进制的规范绝对路径，不能是包装脚本或符号链接。

```json
{
  "schema_version": 1,
  "engine": "codex",
  "executable": "/absolute/native/codex",
  "protocol": "codex-app-server-v2",
  "policy": {
    "context": "standalone-api",
    "system_policy": "must-be-absent",
    "approval_policy": "on-request"
  }
}
```

按[提供商配置指南](PROVIDER-SETUP.zh-CN.md)准备 `/absolute/providers.json`，保持用户选定的实际模型和全部需要保留的质量字段。若使用自定义模型别名，需要提供完整、真实的 `codex_model_catalog` 条目；不能为了加载成功而编造模型能力。模型目录、实际配置和提供商请求三处都要一致。字段见[宿主配置契约](HOST-CONFIG-CONTRACT.en.md)。

```bash
parallel-dev provider validate --file /absolute/providers.json
parallel-dev --state-dir /absolute/controller-state host verify --project my-project --deployment /absolute/codex-deployment.json --provider-config /absolute/providers.json --output /absolute/new-native-verification
parallel-dev --state-dir /absolute/controller-state doctor --engine codex --provider-config /absolute/providers.json
```

验收输出目录必须不存在。验收与调度器使用同一排他锁，已有活动/未知任务必须先对账，不能并行启动另一套验收进程绕过容量管理。`host verify` 需要 operator 身份，并在全部检查通过后才保存控制器拥有的验收记录；没有导入“我已经通过”的布尔值接口。

`doctor` 的退出码 0 只表示查询成功。应检查 `effective_mode`、`native_deployments` 和 `blocking_reasons`。原生协议通过与真实提供商验证分别报告；前者不会把 `real_provider_verified` 改为 true。

## 4. 启动持续队列

```bash
parallel-dev --state-dir /absolute/controller-state run my-project --watch --timeout 3600 --provider-config /absolute/providers.json
```

默认 `run` 会在没有可执行任务时退出。`--watch` 让它继续等待，直到会话超时或控制器被中断；不需要为了新任务反复重启控制器。同一进程的完成事件立即唤醒队列；其他 MCP/CLI 进程写入的新任务通过账本轮询发现，单次空闲等待最多 250 ms；恢复与扫描等工作仍会增加实际发现延迟。源码复制、验收、事务和进程启动仍有实际耗时，因此不承诺毫秒级启动时延。

满槽时停止新准入；某项退出证明到达后，空出的槽位可以立刻用于独立任务或验证阶段。未知实例继续计占用。监听期间不会因重复检查 LOST 实例而不断写入相同失联事件，也不会释放未知实例。

会话超时先停止新增，只取消仍在活动的实例；未准入的排队任务、已提交或审核的成果、项目启用状态保留。下次可以重新运行队列，被取消的活动任务仍需按实际结果重新规划；未知实例不能盲目重放。显式 `cancel PROJECT` 则是更广的项目取消：禁用项目并撤销相关任务与历史门槛，且不会自行停止已经运行的 watch 进程。键盘中断采用既有项目取消语义，应先确认操作意图。`--watch` 不会唤醒已经关闭的协调器聊天，也不会自行调用额外模型创造新需求。

## 5. 让协调器持续发现合理任务

安装目录保留英文 Skill 和 AGENTS 规则。协调器通过 MCP 的只读 Git 接口查看已提交源码，形成独立任务，检查依赖和共享资源，再登记到已批准的计划。

| MCP 工具 | 权限与用途 |
| --- | --- |
| `parallel_status` / `parallel_events` | 读取容量、任务和事件 |
| `parallel_queue` / `parallel_metrics` | 读取有界准入诊断和账本指标；不预留槽位、不建立当前质量证明 |
| `parallel_project_tree` | 分页读取已启用、已有计划项目的指定已提交目录 |
| `parallel_read_file` | 读取同一可达提交中的有界 UTF-8 普通文件 |
| `parallel_register_task` | 仅通过 planner/coordinator/operator 身份暴露，在已有批准范围内登记任务 |

源码读取不访问未跟踪或忽略文件，不跟随符号链接、子模块、过滤器、替换对象或远程惰性取回。分页后续请求使用首批返回的精确提交，避免 HEAD 改变导致混读版本。二进制文件和超大文件返回明确错误。

默认插件 MCP 为 reader。需要登记任务时，由部署者在可信协调器配置中指定 planner 或 coordinator 身份文件；不要把能力文件打包或放进源码树/Worker。

```bash
parallel-dev --state-dir /absolute/controller-state --identity-file /absolute/controller-state/identities/planner.key mcp
```

严格 Hook 对明确属于本插件的协调器 MCP 工具返回空决定，让宿主继续执行原有权限判断；MCP 服务再独立验证登记权限。它不把父会话 ID 当作 Worker 身份。当 Hook 事件的 cwd 位于已启用的受管项目内时，原生 Agent/Task/派发工具及未绑定的普通工具会被拒绝。独立协调器环境不因连接 MCP 就自动获得这层项目 Hook 限制，仍遵守其自己的宿主策略和共享账本。协调器安装配置应位于被登记源码项目及其祖先之外，避免将 `.codex` 或 `.agents` 技能/插件政策引入独立 Worker 配置。项目内已有此类政策时不会自动迁移，也不应删除它们来通过验收。

## 6. 成功、恢复与交付

一次实现只有同时具备成功退出、未取消/未超时、完整宿主终态和有效项目指令，才进入候选冻结。恢复时重新核对 executor 的持久结果和完整 stdout，不能从账本中的退出码猜测日志未截断。

Codex 必须完整报告一次匹配的线程和回合；元数据回退错误会阻断。ZCode 必须有匹配 session/turn、`resultType=success` 和最终结果，取消或预算耗尽不能因投影变成 idle 而通过。

后续仍需真实项目验收命令、组合验证，以及指定独立 reviewer 对精确交付摘要的决定。模型和工具协议的验收，不替代代码质量检查，也不证明并行一定比串行更快。
