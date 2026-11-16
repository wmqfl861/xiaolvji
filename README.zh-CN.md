# Parallel Development — Codex / ZCode 插件完整设计

> 版本说明：本文保留 1.0 设计/交接快照。2026-11-03 已新增 0.1.0 可运行验证版；当前实现、未完成项与真实验收范围以 [运行指南](docs/RUNTIME.zh-CN.md)、[开发状态](docs/DEVELOPMENT-STATUS.zh-CN.md) 和 [验证记录](VALIDATION.json) 为准。

版本：1.0.0 · 设计日期：2026-11-03

## 交付性质

这是可供实施的完整设计包，不是已经编译、安装、实机验收的插件成品。它包含英文指令和 Skill 正式设计源、两端独立的插件与 Hook 模板、任务与事件协议、共享调度器设计、英文 AGENTS 注入参考实现，以及验收和实施计划。

`spec/` 中的 `.example` 文件是配置模板；它们引用的 `bin/parallel-dev` / `parallel-dev.exe` 是本设计要求实现的运行时，当前包不提供该二进制。不要把模板改名后宣称整个调度插件已可用。

## 阅读顺序

1. `DESIGN.zh-CN.md`：完整架构、两种运行模式、并发/隔离/质量机制。
2. `COMPATIBILITY.zh-CN.md`：固定来源、两端真实能力、文档与源码的差异。
3. `spec/AGENTS.block.en.md`：注入的英文项目规则。
4. `spec/parallel-development.SKILL.md`、`spec/roles.en.md`：英文工作流程与角色说明。
5. `spec/codex/`、`spec/zcode/`：原生格式的独立适配模板。
6. `schemas/`、`examples/`：共享协议和示例数据。
   `spec/controller-api.en.md` 和 `spec/hook-contracts.en.md` 定义操作权限、事务和Hook输入输出。
7. `ACCEPTANCE.zh-CN.md`：实现后必须通过的验收，以及本次实际验证记录。
8. `IMPLEMENTATION.zh-CN.md`：适合并行实施的开发任务和依赖。

## 英文注入参考工具

`reference/inject_agents.py` 是独立、可运行的参考工具，只处理标记块注入和撤销，不启动代理、不注册 Hook、不修改全局配置。默认执行 `inspect`。使用真实项目之前，先运行 inspect 并检查实际生效的指令文件和 override。

```sh
python3 reference/inject_agents.py inspect --project /absolute/path/to/project
python3 reference/inject_agents.py apply --project /absolute/path/to/project
python3 reference/inject_agents.py remove --project /absolute/path/to/project
```

Windows 可使用已安装的 `py -3` 或 Python 可执行文件的绝对路径。程序不依赖第三方 Python 库。

参考工具只管理一个显式项目目录中的一个目标文件，不是完整doctor；它不会推导所有祖先/嵌套目录的有效指令，也不解决同一项目中Codex override与ZCode最近AGENTS分别加载的双目标安装。具体限制见 `reference/README.en.md`。完整安装器应按设计解析每个宿主的实际规则来源。

本包所有插件自产的模型指令、Skill、角色提示、Hook 上下文和示例拒绝原因均为英文；中文仅用于面向使用者的设计说明。用户原始需求可保留原语言作为任务数据，用户回复语言不被强制切换为英文。

## 版本范围

- Codex：依据 2026-11-03 官方本地 Codex 文档设计，具体本机版本须由 doctor 和宿主测试确认；不把本地适配声明扩展到 Work Cloud。
- ZCode：依据 `zai-org/ZCode` 固定提交 `a60ba6a7e5a06742270ca605f5d2cc2766afa7e1`，README 标记 v3.17.1。源码、实际加载诊断和测试优先于旧网站说明或工具描述字符串。

未在本次会话中安装到用户的 Codex/ZCode，也未修改用户已有项目规则。
