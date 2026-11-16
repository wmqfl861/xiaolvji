# 实施任务与交接

> 版本说明：本文保留 1.0 设计/交接快照。2026-11-03 已新增 0.1.0 可运行验证版；当前实现、未完成项与真实验收范围以 [运行指南](docs/RUNTIME.zh-CN.md)、[开发状态](docs/DEVELOPMENT-STATUS.zh-CN.md) 和 [验证记录](VALIDATION.json) 为准。

## 1. 当前交付与待开发部分

已交付：完整架构；固定来源兼容性研究；英文AGENTS规则、Skill、角色；两端配置模板；任务/事件/能力schema；示例数据；AGENTS注入参考实现；验收矩阵。

待开发：实际parallel-dev运行时及二进制、持久账本、真实执行器/沙箱、宿主驱动、MCP服务、安装器与doctor、集成审核通道；随后按验收规范做宿主和OS实测。

配置模板引用的程序是待开发目标。当前包不能作为已实现的完整调度插件安装。这一状态不改变设计内容的完整性。

## 2. 阶段与并行划分

| 编号 | 交付 | 依赖 | 写入负责人范围 |
|---|---|---|---|
| P0 | 冻结协议、状态机、权限与能力等级 | 本设计审核 | schemas、公共类型 |
| P1 | 账本、原子准入、资源租约、outbox | P0 | core/registry、core/admission |
| P2 | 英文注入器、Skill封装、策略版本 | P0 | policy、skills、installer/injection |
| P3 | Codex插件/Hook/CLI驱动与doctor | P0 | adapters/codex |
| P4 | ZCode插件/Hook/CLI驱动与doctor | P0 | adapters/zcode |
| P5 | 独立工作树、进程树、沙箱 | P0 | executor、workspace |
| P6 | 调度循环、重启恢复、取消 | P1协议；P3-P5可用测试替身 | core/dispatcher、recovery |
| P7 | 证据、集成队列、外部审核权限 | P0、P1 | evidence、integration、review |
| P8 | 组合运行、故障注入与跨平台验收 | P1-P7 | integration-tests、release |
| P9 | 正式独立审核、版本发布 | P8 | 审核者拥有的发布入口 |

P1-P5在公共协议冻结后可并行；P6可先对契约测试替身开发，但真实验收必须等待驱动。共享schema修改由P0负责人协调，不允许各适配器自行分叉。

运行时语言与二进制打包决策属于P0。设计建议Python标准库原型/SQLite，最终发行可采用平台可执行包；不要在没有需求的情况下引入远程收费调度服务。

## 3. 每项交接必须包含

任务与attempt编号、批准计划版本、基线与最终提交、实际修改路径、接口版本、验证命令与环境/退出码、通过/失败/跳过、已知限制、需下游注意的语义变化。经验只作为带来源和验证状态的候选记录。

## 4. 首个可用里程碑

先完成单项目、单机器、4个受控Worker、两个宿主适配器、独立worktree、共享账本、事件补位、精确候选验证和外部审核门槛。跨机器调度不纳入首版；多个机器的本地SQLite不能被称为同一个全局槽位池。

原生协作模式可先交付用于阅读/调查，但不得用它冒充受控执行模式已完成。严格模式只有实际隔离与故障验收通过后才开放。

## 5. 发布文件布局（目标）

| 路径 | 内容 |
|---|---|
| plugin.json / .codex-plugin/plugin.json | Codex发布manifest |
| .zcode-plugin/plugin.json | ZCode发布manifest |
| skills/parallel-development/SKILL.md | 英文主Skill |
| policies/AGENTS.block.en.md | 英文受管块 |
| hooks/codex.json | Codex原生Hook配置 |
| hooks/hooks.json | ZCode原生Hook配置 |
| bin/parallel-dev 或 parallel-dev.exe | 实际共享程序 |
| adapters/ | 两宿主协议与能力规则 |
| schemas/ | 版本化共享协议 |
| docs/ | 用户文档、兼容性、验收与升级说明 |

建议从同一源码生成两个宿主发行包，避免一个宿主自动发现另一套hooks/hooks.json。公共Skill与策略内容摘要一致，宿主配置分别生成。
