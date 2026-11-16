# 兼容性与证据

> 版本说明：本文保留 1.0 设计/交接快照。2026-11-03 已新增 0.1.0 可运行验证版；当前实现、未完成项与真实验收范围以 [运行指南](docs/RUNTIME.zh-CN.md)、[开发状态](docs/DEVELOPMENT-STATUS.zh-CN.md) 和 [验证记录](VALIDATION.json) 为准。

检查日期：2026-11-03。证据等级：官方文档 / 固定提交源码检查。本次没有运行两端的真实 Agent。

## 1. 能力矩阵

| 项目 | 本地 Codex | ZCode 固定提交 |
|---|---|---|
| 主插件清单 | 根 plugin.json，OpenAI扩展；兼容 .codex-plugin/plugin.json | .zcode-plugin/plugin.json；兼容 .claude-plugin/plugin.json |
| Skill | 插件 skills/<name>/SKILL.md | 同样支持；另有 .zcode/skills、.agents/skills |
| 项目指令 | AGENTS.md；同目录override优先；祖先到cwd累积 | 默认AGENTS.md，最近的项目文件；另读用户级来源 |
| 典型Hook处理器 | command，timeout秒，Windows覆盖字段 | process，command+args，timeoutMs毫秒；也有command兼容 |
| 插件Hook配置 | 按manifest的OpenAI扩展路径 | 自动发现hooks/hooks.json，加上额外显式来源 |
| 项目Hook | 需有效配置层及信任 | 当前源码支持，需项目声明摘要信任；旧网站说明已落后 |
| 子代理Start/Stop Hook | 支持，但Stop不代表线程关闭 | 当前公共事件无这两项 |
| 子代理身份 | 普通工具Hook的session_id可能是父ID | 有Agent结果ID，但启动回执不等于后台完成 |
| 原生工作目录 | 不能假定每个子代理自动隔离 | 普通Agent继承父工作目录，输入无cwd/worktree字段 |
| 子代理Hook覆盖 | 存在覆盖和身份局限 | 普通Agent子runtime不继承父hooks配置，不能保证子工具被拦 |
| 原生后代 | 以实际宿主限制为准 | 普通子runtime禁用subagents，不能任意创建孙代理 |
| 原生并发口径 | 同时打开的子线程，不含主线程 | CreateWorkflow为active ask；另有进程内模型请求治理 |
| 跨进程全局Worker上限 | 由本插件共享控制器另行管理 | 同左，不能把模型限流器当Worker全局账本 |
| 严格文件隔离 | 需真实执行器/沙箱 | 同左，cwd和worktree不是权限边界 |
| 云编排 | 本设计不声明支持Work Cloud本地命令Hook | Web/桌面/CLI执行位置需doctor确认 |

## 2. Codex官方来源

- [插件打包](https://developers.openai.com/plugins/build/plugins)
- [Hooks协议与覆盖边界](https://learn.chatgpt.com/docs/hooks)
- [AGENTS指令发现](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Skill发现](https://learn.chatgpt.com/docs/build-skills)
- [Subagents与权限继承](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [原生配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

新portable manifest中的extensions.com.openai整体取代兼容清单的OpenAI配置，不进行字段拼接。发布应选择明确的一套生成路径，不要把必需内容分散在互相覆盖的清单里。

PreToolUse可覆盖spawn_agent（Agent alias）、Bash/exec_command、apply_patch和已支持MCP/本地工具。write_stdin不重新执行PreToolUse；一些特殊路径和托管工具可能不经过同一检查。SubagentStart用于上下文，不是启动拒绝点。后台Hook不能作为同步准入门槛，也不会凭完成输出自动唤醒空闲对话。

这些边界意味着：原生协作模式不得声称拥有不可绕过的全文件、全子代理强制控制。

## 3. ZCode固定证据

仓库：[zai-org/ZCode](https://github.com/zai-org/ZCode)。固定提交：`a60ba6a7e5a06742270ca605f5d2cc2766afa7e1`。README标记v3.17.1、2026-10-30更新。

以下链接固定到同一提交。网页抓取不能完整读取部分文件，因此本次直接检出了公开仓库并读取源码；没有执行仓库脚本或安装依赖。

### 3.1 Hook与插件

- [当前Hook说明](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/09-hooks.md)
- [当前插件说明](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/10-plugins.md)
- [Hook协议类型](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/hooks/index.ts)
- [字段兼容与插件变量](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/hooks/configured-runner-input.ts)
- [插件Hook来源合并](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/plugins/hook-sources.ts)

当前七个公共事件为SessionStart、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PostToolUseFailure、Stop。不要添加不存在的SubagentStart/Stop。

插件hooks文件顶层为hooks事件表；运行配置则为hooks.enabled与hooks.events。两种包装不能混用。hooks/hooks.json会自动加载，不能再显式声明同一来源导致重复处理。

项目Hook的信任绑定工作区与声明摘要。声明变化重新核对信任。启用或yolo并不等于信任。

固定源码中的process Hook输出解析还存在一项边界：进程退出0但stdout为空、不是对象文本或JSON解析失败时，会静默返回无结果，不能把这些情况都视为onError:deny能够拦截的错误。成功解析JSON后违反输出schema会走不同错误路径。参见[Hook输出解析实现](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/hooks/configured-runner-callback.ts)。

### 3.2 普通Agent的实际限制

- [Agent输入输出](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/tools/agent.ts)
- [子runtime构造](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/runtime/methods/subagent.ts)
- [Hook runner创建条件](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/runtime/helpers/runtime-tools.ts)
- [AGENTS最近文件查找](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/context/index.ts)

从子runtime配置构造与Hook runner创建条件可得：该路径未继承父hooks/hookRunner。此为源码推导结论，应在升级版本重新验明；不能把父会话镜像工具事件误当成已通过父Hook的证据。

普通Agent仅有description/prompt/subagent_type/run_in_background等已定义输入，不能擅加cwd、task_id或permit。任务标识可以写进prompt数据，并在适配器账本建立关联。SendMessage可恢复已结束子代理，因此恢复也必须重新准入。

### 3.3 新旧Workflow不可混用

- [旧Workflow执行器](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/bootstrap/src/app/script-workflow-runtime.ts)
- [CreateWorkflow输入schema](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/tools/create-workflow.ts)
- [动态调度器](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/dynamic-workflow/src/engine/scheduler.ts)

旧Workflow描述提到isolation:worktree和嵌套workflow，但执行代码对这两个请求直接抛出未实现错误。

新的CreateWorkflow支持显式max_concurrency，并在finishLiveNode中减少activeAsks后调用pumpAll，具备滚动补位。它没有逐Agent工作目录/工作树隔离输入，也不能被解释为跨OS进程统一槽位服务。协议DSL与旧Workflow不同，不能复制旧脚本冒充新适配。

插件原生协作模式可优先利用已验明的CreateWorkflow调度；并行写入仍需额外隔离。严格模式使用受控Worker驱动，不依赖未实现的worktree选项。

### 3.4 独立CLI驱动

- [CLI参数](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/arguments.ts)
- [非交互默认模式](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/run.ts)
- [结果输出](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/prompt-command.ts)

已核实cwd、prompt、output-format、mode、workflow-mode、disallowed-tools参数。未确认prompt-file/model/config/max-turns等CLI参数；模板不得虚构。非交互默认模式为yolo，适配器必须显式选择并探测权限策略，不能把默认值当作安全部署配置。

## 4. 必须重新探测的事项

宿主版本、加载清单、英文规则实际来源、Hook信任和失败行为、原生ID关联、恢复事件、完成通知、实例关闭、进程树终止、Windows引号与路径、模型配置继承、沙箱实际写入边界、项目外副作用、最终审核授权隔离。

现有证据足以设计适配层；尚不足以宣称任何用户机器上的端到端安装已通过。
