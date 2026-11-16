# 0.5.0 提供商通道：演示、配置与接入状态

本版已经实现受限提供商代理及隔离通信，并通过真实 Linux 沙箱、真实进程、SQLite 和本地合成 HTTP/SSE 服务验证。**真实 Codex 已通过合成服务的原生协议测试；正式执行要求目标机器通过按策略绑定的 `host verify`。** 当前机器存在无法完整迁移的管理策略，正式原生任务仍被阻断。配置校验、合成原生协议、真实提供商和安装后的插件联调分别验收。

## 1. 运行可复现演示

在源码或解压后的插件目录运行：

```bash
python3 bin/parallel-dev demo --profile controlled_executor --provider-transport --output /tmp/xiaolvji-provider-demo-01
```

目标目录必须不存在。演示使用两个受管槽位完成四个独立或带依赖的模块，然后执行验证与集成。四次合成请求实际通过：私有 HOME 中的模型配置 → 沙箱内 HTTP 客户端 → 私有 loopback → 任务专用 socketpair → 控制器代理 → 本地确定性服务。

这个服务不是模型。其凭据是程序内固定的合成测试字符串，生产代理的 HTTPS 连接路径不会根据配置或环境切换到这个演示连接器。演示不读取现有登录文件，不进行真实 API 调用，不产生模型费用，也不改变宿主权限。

查看 `DEMO-REPORT.json`：

- `status` 应为 `passed`，所有断言为 `true`。
- `maximum_observed_occupancy` 不超过 2；滚动证据显示后续任务在较慢同级任务退出之前启动。
- `provider_transport.http_requests` 为 4，请求均有持久记录，全部授权最终撤销。
- 原始任务停在 `AWAITING_REVIEW`；独立审核不会由演示自动批准。
- `host_or_model_invoked`、`real_host_invocations`、`real_provider_requests` 明确说明证据范围。

默认严格演示没有 `--provider-transport`，继续使用完全离线的策略。无论实现 Worker 是否使用提供商通道，自动生成的所有质量检查都保持离线、只读源码、无提供商授权。

## 2. 明确保存模型与质量配置

新配置文件一次性描述提供商策略、项目/宿主绑定，以及由操作者指定的凭据文件引用。配置中不放 API 密钥。以下内容仅用于展示字段；域名、模型和路径都必须替换为你明确选择的值：

```json
{
  "schema_version": 1,
  "global_request_limit": 4,
  "providers": [
    {
      "profile_id": "selected-provider",
      "api": "openai-responses",
      "upstream_url": "https://provider.example.invalid/v1/responses",
      "model": "EXACT_MODEL_ID",
      "request_constraints": {"reasoning": {"effort": "high"}},
      "max_requests": 32,
      "max_request_bytes": 2097152,
      "max_response_bytes": 8388608,
      "request_timeout": 120,
      "max_concurrency": 4
    }
  ],
  "bindings": [
    {
      "project_id": "my-project",
      "host": {
        "schema_version": 1,
        "engine": "codex",
        "provider_id": "selected-provider",
        "api": "openai-responses",
        "model": "EXACT_MODEL_ID",
        "quality": {"model_reasoning_effort": "high"}
      }
    }
  ],
  "credential_files": {
    "selected-provider": "/absolute/private/provider.key"
  }
}
```

示例中的 `high` 不代表推荐或默认值。应填写原来明确选择的推理级别，以及需要保留的其他已支持参数；模型能否接受这些值仍需要实际宿主/提供商验证。控制器不从环境变量、登录缓存或旧配置猜测它们，不自动改用更便宜的模型，也不会删除不认识的选项以求启动成功。

提供商 `request_constraints` 必须与宿主快照推导出的原生请求参数精确一致，包含类型；整数和浮点数不作为相同配置接受。Codex 支持的配置字段、`service_tier` 映射以及 ZCode 的完整显式模型能力字段见 [宿主配置契约](HOST-CONFIG-CONTRACT.en.md)。ZCode 原生选项编译器已在固定完整源码上执行验证；完整配置 schema 和 CLI 仍未加载，不能把源码映射验证当作完整宿主验收。

## 3. 只校验，不读取凭据

```bash
python3 bin/parallel-dev provider validate --file /absolute/provider-config.json
python3 bin/parallel-dev doctor --engine codex --profile controlled_executor --provider-config /absolute/provider-config.json
```

这两个命令不会读取 `credential_files` 指向的内容，不登录，不发送模型请求。输出包含配置与快照摘要，以及 `credentials_read:false`。`doctor` 查询成功的退出码不表示任务可以运行，必须读取 `effective_mode` 和阻断原因。

正式 `run --provider-config` 在读取凭据和发放授权之前，检查精确部署、配置、运行时与项目指令的现场验收记录。`host verify` 使用真实原生宿主和固定合成服务，不读取 API 密钥；不符合独立 API 策略的机器会在启动夹具之前被阻断。没有跳过该门槛的命令行开关。具体部署 JSON、可选的精确 `codex_model_catalog`、验收和 `run --watch` 步骤见 [原生宿主接入](NATIVE-HOST.zh-CN.md)。

实际代理启动时，仅读取明确指定的私有凭据文件：当前控制器用户所有、普通文件、单链接、无符号链接别名、无组或其他用户访问权限、内容是有界 ASCII token。开发源码、工作区、隐式可读工具链及 Python 运行时挂载内的凭据文件会被拒绝；只读文件仍可能被同一映射 UID 读取，`0600` 本身不能保护这类位置。API 密钥只用于控制器到提供商的 HTTPS 请求；Worker 收到的是每次任务单独生成的可撤销 capability。

## 4. 三种容量必须分别理解

| 计数 | 何时占用与回收 | 能证明什么 |
| --- | --- | --- |
| 受管任务槽 | 原子准入时占用；可信进程树退出证明后释放 | 本资源域内受管实现、验证和集成的并发上限 |
| 本地提供商在途请求 | 发出请求前计数；本地连接与受管处理结束后释放 | 本代理中的全局、单提供商、单任务请求并发 |
| 提供商远端计算 | 由外部提供商决定 | 断开 HTTP、取消任务或控制器退出，均不能证明远端计算已经停止 |

请求数预算在连接提供商之前持久扣减。失败、超时和断线不会退款；重启不会重放未完成请求。令牌速率、费用上限和远端计算硬配额不由这些计数自动实现。

## 5. 原生验收与真实提供商的区别

本机 Codex `0.167.0-alpha.4` 已在真实受控执行器中完成两次合成模型请求、一次原生工具调用及结果返回；另一个实际测试覆盖 `exec_command` → `write_stdin` 的三次请求链。固定夹具验证 13 项边界，包括精确模型/质量、无原生派发工具、允许范围内写入、兄弟文件和 Git 写入被拒绝、外网不可达、控制器文件不可见、完整结果及进程树回收。

该适配器使用官方 App Server 的 `externalSandbox` 接口，由既有外层 Linux 沙箱承担隔离。它不能等价保留所有原生审批规则，故首个正式配置只接受明确独立的 API 部署；存在管理要求、项目原生配置、技能或插件目录时保持阻断，不删除这些策略以求通过。本机 `/etc/codex` 存在，真实 CLI 已验证在读取凭据之前拒绝。详情见 [Codex 证据](CODEX-COMPATIBILITY.en.md)。

真实外部提供商的认证、实际流式格式和模型质量表现尚未测试；合成响应不会证明这些事实。ZCode 完整公开源码已重建，局部原生映射执行成功；完整构建受到 npm 访问限制，详见 [ZCode 证据](ZCODE-COMPATIBILITY.en.md)。
