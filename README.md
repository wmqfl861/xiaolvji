# xiaolvji · 并行开发控制器

**0.6.0 增加了可直接安装的本地 Codex 插件市场。** 小绿鸡让主 Agent 按任务依赖和写入范围调度多个开发任务。真实 Codex CLI 已能从已安装的包读取协作 Skill、七个 Hook 和六个只读 MCP 工具。配置自定义控制器目录时，本地包及兼容覆盖层都会将目录传给 MCP。

下载完整交付后，从 `plugins/` 选择与系统对应的 `parallel-development-codex-marketplace-*`，将内层 ZIP 解压到一个单独目录。在已初始化控制器的终端中运行以下命令。使用自定义目录时，先设置 `PARALLEL_DEV_STATE_DIR`。[安装与诊断指南](docs/HOST-INSTALL.zh-CN.md)

```bash
codex plugin marketplace add /absolute/path/to/unpacked-marketplace
codex plugin add parallel-development@xiaolvji-local
python3 /absolute/path/to/unpacked-marketplace/bin/parallel-dev host inspect-plugin --marketplace /absolute/path/to/unpacked-marketplace
```

Linux 上用真实进程验证了安装与服务发现。七个 Hook 在新安装中均等待宿主信任。当前测试环境在创建 Codex 会话时受到系统沙箱限制，真实提供商开发任务和完整 ZCode CLI 仍待现场验收。[本版验证记录](validation/codex-installed-plugin.json)

## 先运行完整演示

需要 Python 3.11+、Git，以及支持 `/proc`、pidfd 和 child subreaper 的 Linux 环境。运行时没有第三方 Python 依赖。

```bash
python3 bin/parallel-dev --version
python3 bin/parallel-dev doctor
python3 bin/parallel-dev demo --output /tmp/xiaolvji-demo-01
python3 bin/parallel-dev demo --profile controlled_executor --output /tmp/xiaolvji-strict-demo-01
python3 bin/parallel-dev demo --profile controlled_executor --provider-transport --output /tmp/xiaolvji-provider-demo-01
```

`demo` 要求一个尚不存在的目录。它创建独立仓库，在两个槽位下执行四个模块、技术检查和组合检查，核验长任务未结束时的滚动补位，并停在独立审核门槛。它执行真实 Git 与进程，不调用模型或 Codex/ZCode。报告明确区分这两种验证。

`--provider-transport` 额外启动固定的本地合成服务，实际走过模型代理、私有通道和隔离 HTTP 客户端。它核对四次请求的记账、请求上限和授权撤销，不读取账户凭据、不访问真实提供商。

严格演示额外要求非 setuid 的 bubblewrap 0.12.0+、util-linux `unshare`、可用的非特权命名空间及支持的原生 ABI。每次启动都验证实际边界；缺少能力便阻断。完整策略和限制见 [Linux 隔离契约](docs/STRICT-SANDBOX-CONTRACT.en.md)。

## 已实现的关键行为

- 同一资源域共享一个 SQLite 账本；全局和项目上限、任务唯一活动实例、写入范围和独占资源在一个事务内准入。
- 调度优先级和质量角色加分共同按登记年龄老化到有限上限，随后先登记者优先；持续到来的高优先级任务不能无限插队，质量门槛仍逐项检查。
- `queue` / `parallel_queue` 解释容量、依赖、范围和资源阻塞；预览不预留槽位，真实准入仍在同一原子事务重新检查。
- `metrics` / `parallel_metrics` 统计持久事件中的等待、占槽、释放间隔、重试和历史质量记录；超预算或历史不一致明确报错，不从占槽率推导提速或质量结论。
- 满槽时停止新增；每次确认进程树退出后立即释放占用并唤醒队列。验证和集成检查也使用同一受管任务容量池。
- `run --watch` 在空闲期等待已批准范围内新登记的任务；同进程完成事件立即唤醒，其他进程登记的任务通过空闲轮询发现，单次空闲等待最多 250 ms；恢复、扫描和系统工作还会影响实际发现延迟。超时停止新增并执行既有取消流程。
- 原生验收绑定实际二进制、运行时代码、系统工具、内核启动身份、项目指令和精确模型配置；变更使记录失效，不接受调用者自报“已测试”。
- 原生成功还要求完整、身份一致、未截断的宿主终态；退出码 0、模型声称完成、ZCode 的 idle 投影均不能单独产生候选。
- 一个控制器拥有队列；持久化启动 outbox、唯一 claim、进程身份和重启对账防止重复启动。未知实例保持占用。
- 私有 Git clone 和独立候选仓库隔离可变 Git 元数据；检查修改、重命名两端、删除、忽略文件和符号链接越界。
- 严格离线 Worker 使用只读源码基底和显式可写范围；不挂载宿主 HOME、控制器账本、审核凭据或其他工作区。先持久化实际隔离证明，再放行任务命令。
- 严格检查逐条运行在独立只读源码沙箱中；报告服务位于检查沙箱外，检查命令不能修改自己的报告或退出证明。
- 模型代理固定提供商 HTTPS 地址、API、模型和质量约束，限制每次任务的请求数、字节数、期限及本地在途请求数；请求之前持久扣减预算，失败不退款、不盲目重放。
- 取消、授权失效或控制器退出会关闭本地模型通道；重启撤销旧授权。模型请求结束不替代进程树退出证明，断开连接也不证明远端计算已停止。
- 隔离宿主配置由显式快照生成，只读挂载到私有 HOME；不自动采集现有账户或猜测模型参数。未知配置或质量不匹配时拒绝启动。
- 使用冻结契约、实现验证、集成验证、最终接受四类依赖门槛。需要先集成的下游任务通过已验证任务的阶段集成解锁。
- 记录实际消费的不可变输入版本；后续集成不会改写旧输入的含义。计划、候选、契约或审核权限失效时撤销相关证明。
- 检查报告绑定同一提交、树摘要、计划、命令、环境摘要、日志和退出状态。缺少必需检查不能算通过。
- 组合检查通过后以 CAS 更新 `refs/heads/parallel-dev/integration`，保留用户主分支、工作区和暂存区。发布使用单独的持久租约保护计划版本。
- 正式接受仅由指定 reviewer 身份记录；旧交付的批准不会批准新组合交付。
- Codex / ZCode 分别生成插件发现结构；英文政策注入保留原有文件字节，升级/卸载具有所有权检查。Hook 不自动授予权限，也不凭模型结束消息回收槽位。

## 文档入口

| 文档 | 内容 |
| --- | --- |
| [调度与指标](docs/SCHEDULING.zh-CN.md) / [英文契约](docs/SCHEDULING-CONTRACT.en.md) | 防饥饿规则、准入诊断、统计口径、查询预算与权限 |
| [运行指南](docs/RUNTIME.zh-CN.md) | 从初始化、项目启用、计划和任务登记到运行、取消、审核的完整操作 |
| [宿主安装](docs/HOST-INSTALL.zh-CN.md) | Codex / ZCode 发现路径、本地市场与通用源码包、Hook 信任与 MCP 配置 |
| [能力与限制](docs/LIMITATIONS.zh-CN.md) | 执行边界、未验收能力、暂停与恢复行为 |
| [开发状态](docs/DEVELOPMENT-STATUS.zh-CN.md) | 蓝图 P0–P9 与本次实现、剩余工作对应关系 |
| [验证记录](VALIDATION.json) | 实际运行的测试、演示、环境和未运行项目 |
| [需求](REQUIREMENTS.zh-CN.md) / [蓝图](BLUEPRINT.zh-CN.md) | 原始完整目标与后续开发方向 |
| [设计](DESIGN.zh-CN.md) / [设计阅读版](DESIGN.html) | 架构设计快照；当前实现状态以运行指南和验证记录为准 |
| [公共运行时契约](docs/RUNTIME-CONTRACT.en.md) | 模块 API、权限与状态语义 |
| [Linux 隔离契约](docs/STRICT-SANDBOX-CONTRACT.en.md) | 命名空间、系统调用、可写挂载、启动证明及范围限制 |
| [Codex 本机兼容性](docs/CODEX-COMPATIBILITY.en.md) | 安装版本、配置探测与离线联调的实际结果 |
| [提供商配置与演示](docs/PROVIDER-SETUP.zh-CN.md) | 新通道的运行入口、配置示例和仍然保留的宿主门槛 |
| [提供商通道契约](docs/PROVIDER-TRANSPORT.en.md) / [宿主配置契约](docs/HOST-CONFIG-CONTRACT.en.md) | IPC、请求预算、撤销、配置字段及证据边界 |
| [原生接入指南](docs/NATIVE-HOST.zh-CN.md) / [原生执行契约](docs/NATIVE-HOST-CONTRACT.en.md) | 现场验收、独立部署、策略拒绝、持续运行和结果恢复 |
| [ZCode 源码兼容性](docs/ZCODE-COMPATIBILITY.en.md) | 完整源码重建、真实选项编译器验证与实际构建阻断 |

## 用于自己的项目

先阅读运行指南。项目启用与宿主 Hook 信任是不同操作，构建插件包不会自动授予任何权限。

```bash
python3 bin/parallel-dev --state-dir /absolute/controller-state init --global-limit 4
python3 bin/parallel-dev --state-dir /absolute/controller-state project enable my-project /absolute/repository --limit 4 --profile controlled_executor
```

读取已有资源域的队列与指标：

```bash
python3 bin/parallel-dev --state-dir /absolute/controller-state queue --project my-project --limit 100
python3 bin/parallel-dev --state-dir /absolute/controller-state metrics --project my-project
```

项目启用命令启用严格项目，但不会自动授予宿主运行权限。按[原生接入指南](docs/NATIVE-HOST.zh-CN.md)提供部署描述、精确模型配置，并运行现场验收。存在无法保留的管理策略、未知模型或缺少隔离能力时，保持阻断。

登记已批准的计划和任务后，使用 `run my-project --watch --provider-config /absolute/providers.json`，或同一模式下的 `run --all --watch --provider-config /absolute/providers.json`。主 Agent 按英文 Skill 拆分独立任务，通过已认证的 MCP 登记；程序持续负责准入、补位、质量阶段和证据。`--watch` 保持调度进程运行，不会自动唤醒已经退出的主 Agent 会话，也不会创造新需求。

## 开发验证

```bash
python3 scripts/validate_runtime.py --report /tmp/xiaolvji-validation.json
```

命令运行运行时回归、已有注入器回归和设计静态检查，输出实际结果。不会调用模型、登录账户、修改宿主信任或进行远端发布。

## 构建插件与完整交付包

```bash
python3 bin/parallel-dev package --adapter codex --platform posix --output /tmp/xiaolvji-codex
python3 bin/parallel-dev package --adapter zcode --platform posix --output /tmp/xiaolvji-zcode
python3 scripts/build_release.py --output /tmp/xiaolvji-runtime-0.6.0.zip
```

完整交付包含运行时源码、测试、Git bundle 和六个插件 ZIP。Codex 本地市场包、Codex 通用包和 ZCode 包均提供 POSIX 与 Windows 布局。Windows 执行仍待实机验收。

本轮在 0.5.0 开发分支的源码基础上进行。[0.6.0 开发分支](https://github.com/wmqfl861/xiaolvji/tree/feat/codex-install-v0.6.0)
