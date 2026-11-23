# xiaolvji 0.6.0 运行指南

本版本已经包含可执行的本地控制器：它保存任务图，原子预留并发容量和独占资源，启动独立 Git 副本中的任务，逐个处理完成事件，并把释放的容量用于后续工作。实现、验证、集成和最终审核分别记录状态；任务说“完成”不会直接释放容量或获得接受。

**已实现持续队列、严格 Linux 执行、受限提供商通道，以及按部署绑定的原生宿主验收。** 真实 Codex 二进制已通过合成提供商的完整工具调用链，ZCode 的固定源码选项映射已执行验证。正式 Codex 路径要求目标机器的独立 API 部署通过 `host verify`；当前机器存在不能完整保留的管理策略，因此正式任务保持阻断。本地 Codex 市场包已完成安装和宿主工具发现。会话内调用、实际 Hook 触发、真实外部提供商与完整 ZCode CLI 仍待验收。API 密钥留在控制器，模型和质量值来自明确快照；所有质量检查继续离线。接入步骤见 [原生宿主指南](NATIVE-HOST.zh-CN.md)，边界见 [能力与限制](LIMITATIONS.zh-CN.md)。

## 严格离线后端的入口

```bash
python3 bin/parallel-dev demo --profile controlled_executor --output /tmp/xiaolvji-strict-demo-01
python3 bin/parallel-dev demo --profile controlled_executor --provider-transport --output /tmp/xiaolvji-provider-demo-01
python3 bin/parallel-dev check-runner --manifest /absolute/check-manifest.json --execution-profile controlled_executor
```

完整演示使用同一受管容量池执行实现、验证和集成，保留正式审核门槛。独立 `check-runner` 是低层诊断入口，不会自行登记受管任务或占用另一个账本槽位；受管流水线通过可信服务调用它。严格检查只读源码，报告在沙箱外生成，临时输出放在私有 `/tmp`。默认工具链之外的可执行文件会阻断，不会通过另一个 PATH 同名文件替代。

`--provider-transport` 使用固定的本地合成服务，验证请求代理、一次任务专用 IPC、私有 loopback、预算和授权撤销，不调用真实模型。显式配置的 `provider validate`、`doctor --provider-config` 与 `run --provider-config` 接口见 [提供商配置指南](PROVIDER-SETUP.zh-CN.md)；配置校验不会读取凭据，也不会解开尚未验证的原生宿主门槛。

可写范围必须是已经存在的单文件或已由控制器建立的目录。新文件应属于已批准的目录范围；不存在的精确文件范围不会扩成父目录权限。单文件挂载支持直接写入，不支持原子替换或删除挂载点。要求 bubblewrap 0.12.0+、util-linux `unshare` 及可工作的 Linux 隔离能力；详见 [隔离契约](STRICT-SANDBOX-CONTRACT.en.md)。

## 持续接收新任务

`run` 默认在当前队列无法继续且无活动实例时退出。需要协调器在同一会话内持续拆分已批准范围并补充任务时，使用：

```bash
parallel-dev --state-dir /absolute/controller-state run my-project --watch --timeout 3600 --provider-config /absolute/providers.json
```

`--watch` 在空闲时继续等待；本控制器的完成事件立即唤醒调度，其他 CLI/MCP 进程登记的新任务通过账本轮询发现，单次空闲等待最多 250 毫秒；实际发现还需考虑恢复、扫描和系统工作耗时。达到上限后停止新增准入，正在运行的任务继续执行；未知退出仍占槽。总超时是会话截止时间，达到后停止新准入，只取消仍活动的执行实例。尚未准入的任务、已提交成果与审核记录、项目启用状态都保留，后续可以重新运行队列。被取消的活动任务仍需按实际结果重新规划；未知退出继续占槽。它没有唤醒已退出的宿主会话或自动生成额外需求的能力。

默认 reader MCP 还提供 `parallel_project_tree` 和 `parallel_read_file`，让协调器读取已提交源码；有 planner 身份的协调器可调用 `parallel_register_task`。按提交摘要分页并限制输出，不读取未提交文件、链接目标或任意宿主路径。操作与权限制约见 [原生宿主指南](NATIVE-HOST.zh-CN.md)。

## 1. 准备运行环境

需要 Python 3.11+ 和 Git。执行网关当前要求 Linux `/proc`、child subreaper 和 pidfd 能力；`doctor` 会检查。调度器和账本没有第三方 Python 运行时依赖。

在源码目录直接使用：

```bash
python3 bin/parallel-dev --help
python3 bin/parallel-dev --version
```

也可安装控制台命令：

```bash
python3 -m pip install --no-deps .
parallel-dev --help
```

`--no-deps` 不安装运行时依赖；pip 的构建环境仍可能需要 setuptools。离线或不希望安装时直接使用 `python3 bin/parallel-dev` 即可。以下示例使用 `parallel-dev`，未安装时用前述直接启动方式替换。

实际开发任务还需要对应的 Codex 或 ZCode CLI，并在宿主自己的正常流程中完成配置和认证。本程序不会代为登录，也不会更改模型、推理级别或授予宿主信任。

## 2. 先运行一个不调用模型的演示

```bash
parallel-dev demo --output /absolute/path/to/new-demo-directory
```

目标目录必须尚不存在。演示会创建专用 Git 项目、两个受管槽位、多个真正的 Python 进程，以及实现后的验证和集成任务。较慢的任务尚未结束时，其他任务完成释放的容量可以被后续任务使用；一个下游任务还会使用已验证的上游产物。演示输出 `DEMO-REPORT.json` 和 `DEMO-EVENTS.json`，方便核对容量和事件顺序。

演示不会调用 Codex、ZCode 或模型服务；它验证的是控制器、真实进程生命周期、Git 副本、证据和集成流程。演示的交付仍等待指定独立审核，不会自动伪造最终批准。

## 3. 为一个资源域建立共享账本

同一台机器、同一受管资源域的项目应使用同一个状态目录。不要为每个项目分别创建“全局”账本，否则它们不会共享同一上限。

```bash
parallel-dev --state-dir /absolute/path/to/controller-state init --global-limit 4
```

默认目录为 `~/.local/state/xiaolvji`。也可通过 `PARALLEL_DEV_STATE_DIR` 指定；让 CLI、宿主 Hook 和 MCP 继承相同值，才能查看同一个资源域。`--state-dir` 和 `--identity-file` 都是全局参数，放在子命令之前。

状态目录必须在开发项目和受管工作副本之外。初始化生成 SQLite 账本和独立角色凭据；标准输出列出身份文件路径，不打印秘密值。已有账本的全局上限不会因为再次执行初始化而被静默改写。

| 角色 | 用途 |
| --- | --- |
| operator | 项目策略、明确批准范围和本地管理 |
| planner / coordinator | 在批准范围内登记任务和协调派发 |
| executor | 记录受管实例启动和真实退出事实 |
| verifier | 提交绑定精确候选的技术验证 |
| integrator | 记录实际组合验证并更新专用集成引用 |
| reviewer | 对精确交付摘要作最终决定 |
| reader | 读取状态、事件及已登记项目的固定提交源码 |

普通管理 CLI 默认使用 operator 身份；随包 MCP 默认使用 reader。Worker 不收到控制器凭据。协作模式下，同一 OS 用户的任意进程仍可能读取同用户文件。严格沙箱不会挂载这些凭据；提供商授权只是单次任务的可撤销 capability。真实模型任务仍等待原生宿主验收，不能据此改成协作模式。

## 4. 检查能力并登记项目

```bash
parallel-dev doctor --engine codex
parallel-dev doctor --engine zcode
```

`doctor` 返回 JSON。查询退出码 0 只表示查询完成；是否可用要看 `effective_mode`、`blocking_reasons` 和各项证据等级。`documented`、`source_verified`、`detected` 与 `integration_tested` 含义不同。

默认登记严格配置：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state project enable my-project /absolute/path/to/project --limit 4
```

它保存严格项目配置。正式 Codex 任务另外要求精确提供商配置与新鲜的 `host verify` 记录；本机管理策略和 ZCode 完整 CLI 的当前限制见 [接入指南](NATIVE-HOST.zh-CN.md)。`doctor.offline_executor_supported` 可以为 true，而模型任务的 `effective_mode` 仍为 blocked。配置、二进制、运行时、项目指令或管理策略变化会重新触发检查；不会自动换用较弱模式。

如果项目所有者明确接受可信同用户协作的边界，使用显式选项：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state project enable my-project /absolute/path/to/project --limit 4 --profile cooperative_native --acknowledge-cooperative
```

缺少 `--acknowledge-cooperative` 时，协作模式登记会被拒绝。可用容量同时受全局上限、项目上限、占用实例、依赖和资源冲突约束。验证与集成进程也消耗受管槽位。

同一资源域内不允许两个项目根目录互相包含或指向同一目录。若要修改已启用项目的策略，先禁止新增准入，并确认旧实例退出；未完成的发布事务也必须先恢复：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state project disable my-project
```

`disable` 停止新增准入，不代表杀死已运行任务。需要结束任务时使用后文的 `cancel`。

## 5. 登记真正批准的范围

任务必须绑定当前计划的 ID、revision 和 scope_digest。一个项目的计划 ID 保持不变，后续修订递增 revision；不能用修改旧任务内容的方式绕开版本约束。

简单计划可直接创建。以下路径只是示例，应替换为本次明确批准的范围：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state plan create my-project --id my-project-plan --revision 1 --scope src/parser:directory --scope tests/parser:directory --scope src/formatter:directory --scope tests/formatter:directory --approval-ref "reference-to-the-actual-user-approved-scope" > approved-plan.json
```

`approval_ref` 指向真实的批准记录或用户指令。填入任意字符串不会使超出实际授权的工作变得合理；该字段记录已由可信规划入口确认的批准来源。

需要冻结接口版本时，用 JSON 计划：

```json
{
  "id": "my-project-plan",
  "revision": 1,
  "approved_scope": [
    {"path": "src/parser", "kind": "directory"},
    {"path": "tests/parser", "kind": "directory"}
  ],
  "scope_digest": "sha256:REPLACE_WITH_THE_REAL_SCOPE_DIGEST",
  "approval_ref": "reference-to-the-approved-plan",
  "contracts": [
    {
      "id": "parser-api",
      "version": "1.0.0",
      "digest": "sha256:REPLACE_WITH_THE_FROZEN_CONTRACT_DIGEST"
    }
  ]
}
```

`scope_digest` 是 `approved_scope` 的规范 JSON 摘要，不是整份计划文件的摘要。程序要求完全匹配。可以在源码环境计算：

```bash
PYTHONPATH=src python3 - <<'PY'
import json
from pathlib import Path
from xiaolvji.contracts import digest
path = Path('plan.json')
plan = json.loads(path.read_text(encoding='utf-8'))
plan['scope_digest'] = digest(plan['approved_scope'])
path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
parallel-dev --state-dir /absolute/path/to/controller-state plan register my-project --file plan.json
```

计划修订前必须确认旧实例已经退出，并处理未完成的集成发布。修订会使旧任务和旧门槛失效；它不会把旧检查结果移植到新范围。

## 6. 登记明确归属的任务

一项任务写明目标、基线提交、读入的契约版本、依赖、写入路径、独占资源和可执行验收。任务文件使用 [task.schema.json](../schemas/task.schema.json)。同一任务 ID 的定义不可改写；返工或变更应登记新 ID。

下面的脚本从第 5 节生成的 `approved-plan.json` 读取真实计划摘要，从项目 Git HEAD 读取真实提交，生成一份可编辑的任务定义。执行前应先在项目中提交需要作为起点的文件；未提交修改不属于该基线。

```bash
python3 - <<'PY'
import json
from pathlib import Path
import subprocess
project = Path('/absolute/path/to/project')
plan = json.loads(Path('approved-plan.json').read_text(encoding='utf-8'))
base = subprocess.check_output(['git', '-C', str(project), 'rev-parse', 'HEAD'], text=True).strip()
task = {
    'schema_version': 1,
    'task_id': 'parser-v1',
    'plan': {key: plan[key] for key in ('id', 'revision', 'scope_digest')},
    'role': 'worker',
    'engine': 'codex',
    'objective': 'Implement the agreed parser behavior. Preserve the frozen interface and existing regression requirements.',
    'base_commit': base,
    'contract_versions': [],
    'depends_on': [],
    'write_scope': [
        {'path': 'src/parser', 'kind': 'directory'},
        {'path': 'tests/parser', 'kind': 'directory'}
    ],
    'exclusive_resources': ['module:parser'],
    'acceptance': [{
        'id': 'parser-regression',
        'criterion': 'The agreed parser behavior and regression tests pass on the exact candidate.',
        'argv': ['python3', '-B', '-m', 'unittest', 'discover', '-s', 'tests/parser'],
        'cwd': '.',
        'timeout_ms': 120000,
        'required': True
    }],
    'expected_artifacts': ['candidate_commit', 'changed_paths', 'validation_report'],
    'priority': 60,
    'estimated_seconds': 1200,
    'retry': {'max_retries': 2, 'backoff_ms': 2000},
    'final_review_required': True
}
Path('parser-task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
parallel-dev --state-dir /absolute/path/to/controller-state task register my-project --file parser-task.json
```

这是结构完整的操作示例，不保证该示例测试命令适合你的项目。替换目标和检查，确认测试确实能覆盖原有行为；不能用一句“测试通过”代替可执行验收。ZCode 任务将 `engine` 改为 `zcode`。协作模式沿用宿主环境；严格提供商路径需要明确的 ZCode 模型/质量及完整能力快照，不自动继承旧配置。

独立 formatter 任务可以使用另一个 ID 和互不重叠的路径。公共入口、锁文件、共享 schema、迁移、同一端口或数据库命名空间存在关联时，应明确顺序或使用相同独占资源名。资源名是精确匹配的全局字符串，程序不会自动猜测两个不同名字指向同一个外部资源。

目录前缀和精确文件受路径冲突检查；任意 glob 不被当作授权范围。规范化还会检查路径别名、目录祖先关系、越界和链接。提交审计覆盖新增、修改、删除以及重命名两端，独立 Git 元数据避免子任务通过共享 worktree Git 目录改写其他分支。

### 依赖与接口冻结

`depends_on` 每项指定 task_id 和 gate；可附带要求的 artifact_digest。`contract_ready` 表示经过规划入口确认的冻结契约，`implementation_verified` 表示精确候选通过技术检查。实现依赖会将已验证上游对象组合进下游的有效源码基线，不能仅根据上游说“完成”启动。

冻结契约门槛由可信规划入口登记，例如：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state contract-ready my-project parser-contract --digest sha256:ACTUAL_64_HEX_DIGEST --evidence-ref actual-frozen-contract-record
```

先登记相应契约任务，使用真实摘要；任务引用的契约必须与当前计划批准的版本一致。程序支持门槛版本检查，但接口是否足够独立仍需要规划和评审判断。下游需要 `integrated_verified` 或 `final_accepted` 时，流水线可以先集成已经验证的上游集合，避免等所有下游实现后才集成。`final_accepted` 仍需指定 reviewer 对该精确阶段交付作真实决定；审核等待期间，默认 `run` 会退出，审核后重启；`run --watch` 可以继续等待到明确截止时间。后续组合形成新的交付摘要时，不自动继承旧摘要的最终批准；细节见 [能力与限制](LIMITATIONS.zh-CN.md)。

## 7. 运行、观察与取消

```bash
parallel-dev --state-dir /absolute/path/to/controller-state run my-project --timeout 3600 --provider-config /absolute/providers.json
```

上面的严格原生任务需要已通过 `host verify` 的同一配置；配置不会从验收记录中自动加载。需要跨空闲期继续接收任务时加入 `--watch`。明确选择的协作模式不使用严格提供商配置。

要让一个 Dispatcher 在同一共享账本内调度所有已启用项目：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state run --all --timeout 3600 --provider-config /absolute/providers.json
```

`--all` 与项目 ID 互斥，且所有已启用项目必须采用同一显式执行 profile；混合严格/协作模式会被拒绝，不会挑选宽松配置替代其他项目。各项目仍受自己的上限和资源限制。

进度事件写到 stderr，最终状态 JSON 写到 stdout。`run` 在本次调用期间持续运行滚动队列：把可用槽位用于已授权且资源兼容的工作；一个任务真正退出后立即进入释放和补位流程，不等待同批最慢任务。该“立即”是事件触发的调度语义，不是硬实时延迟承诺。

实现结束后自动登记验证任务。验证完成后，流水线发现当前计划的可集成候选，登记组合验证任务；若下游等待集成或最终审核门槛，可先组合已经验证的上游集合；这些工作也经过相同账本，而不是绕开上限启动。内部验证任务有较高优先级；所有角色还共同受有界老化排序约束，防止不断到来的高优先级工作让旧任务一直等待。所有准入和质量规则保持有效，具体公式见[调度与指标](SCHEDULING.zh-CN.md)。

另一终端可以读取状态和事件：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state status --project my-project
parallel-dev --state-dir /absolute/path/to/controller-state events --after 0 --limit 100
parallel-dev --state-dir /absolute/path/to/controller-state queue --project my-project --limit 100
parallel-dev --state-dir /absolute/path/to/controller-state metrics --project my-project
```

`queue` 区分 eligible、当前可派发性和具体阻塞；单项 dispatchable 不代表所有返回项能同时启动。`metrics` 是有界、只读的账本历史统计，不记录最终审核，也不把历史 gate 当成当前有效证明；口径和错误处理见[调度与指标](SCHEDULING.zh-CN.md)。

重点查看 capacity 的 used、available、uncertain，active_attempts 的状态，以及 tasks 的 state、blocked_reason 和 gates。RESERVED、STARTING、RUNNING、STOPPING、LOST 都可能继续占用安全容量。进程退出与交付审核是两套状态；已回收槽位的任务仍可能等待审核。

明确取消任务或整个项目：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state cancel my-project --task parser-v1
parallel-dev --state-dir /absolute/path/to/controller-state cancel my-project
```

取消先阻止相关新准入，再请求受管进程停止，必要时按身份核对后的 pidfd 终止后代。只有确认整个受管进程树结束才释放容量。不会仅凭旧 PID、心跳过期或模型结束消息杀进程和回收槽位。会话超时只取消仍活动的实例，保留未准入任务、已完成证据和项目启用状态。显式取消整个项目则禁用项目并撤销相关任务及历史门槛；它不自动终止一个已存在的 watch 进程。

`--no-auto-verify` 和 `--no-auto-integrate` 可用于明确暂停后续阶段；这不会让缺少证据的任务被视为已通过。0.5.0 没有通用“恢复已跳过自动阶段”的独立命令，正常完整交付应保留默认自动阶段。

同一资源域只允许一个活动 Dispatcher。再次运行会得到 `CONTROLLER_RUNNING`；这不妨碍其他终端读取状态、登记已授权任务或发出取消。当前调用退出后不会常驻后台，也不会唤醒已经空闲的宿主聊天。

## 8. 验证证据、集成引用与最终审核

实现进程退出成功后，控制器先审计写入范围，再创建候选；Worker 自己创建的 Git 提交不是直接信任的交付。验证使用精确候选副本，每个检查保存 argv、cwd、起止时间、退出码、超时和进程树事实、日志路径与摘要、环境摘要、计划及契约绑定。

检查没有 argv 时，必需项标为 `not_run`，可选项标为 `skipped`；两者都不会被写成 `passed`。任何实际失败都阻止顶层技术通过。日志每条 stdout/stderr 流上限为 4 MiB，截断在报告里标明。环境摘要不记录环境变量的秘密值，也不声称环境完全可复现。

验证要求起始 checkout 与候选完全一致，包括未跟踪文件检查；检查运行后不得改变源码。临时文件和缓存默认放在本次验证的外部临时目录。需要在源码相对目录产生构建输出时，可显式使用新建的 `.parallel-dev-check-tmp`，它仅供本次检查，确认后代进程退出后由运行器清理；预先存在的同名目录不会被接管。普通构建目录、pytest 缓存或其他新文件如果留在源码树中，会导致验证/写入审计失败，应配置相应工具的输出位置。

自动集成先组合候选，再在实际组合上重跑规定检查。干净的文本合并不等于验证通过。通过后只更新原项目的专用引用 `refs/heads/parallel-dev/integration`，采用预期旧提交的 compare-and-swap；不把用户当前工作目录 checkout 到新提交，也不直接更新主分支。引用已被工作树 checkout 或基线变化时，发布会拒绝。

发布意图和收据持久记录。若 Git 引用更新成功但后续记录中断，恢复会核对同一个发布 ID、精确候选和当前引用后补全记录；未解决的发布租约阻止计划修订或策略切换。不要手工删掉这些记录来“解锁”。

典型产物位置都在控制器状态目录：

| 目录 | 内容 |
| --- | --- |
| `executor/launches/<attempt>/` | 启动身份、退出证明和受管命令日志 |
| `workspaces/<attempt>/` | 独立源码副本、所有权记录及候选对象 |
| `quality/<attempt>/` | 检查 manifest、报告与检查日志 |
| `candidates/` | 已审计的候选及版本绑定 |
| `deliveries/` | 集成发布收据、交付摘要和审核状态 |
| `integration-attempts/` | 集成失败记录或持久发布意图 |

集成完成后，使用 status/交付收据中的**精确交付摘要**，由配置的独立 reviewer 作决定：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state --identity-file /absolute/path/to/controller-state/identities/reviewer.key review my-project --delivery-digest sha256:ACTUAL_DELIVERY_DIGEST --decision approve --record actual-independent-review-record
```

`--decision` 还支持 `reject`、`rework`；可重复 `--task` 限定本次任务集合。未指定任务时，CLI选择具有已验证集成门槛的任务，且要求它们绑定同一交付摘要。operator 或 Worker 不能假冒 reviewer 完成此操作。是否真正由独立的人或指定外部评审系统审查，属于部署工作流的职责；本地密钥和命令不会自动构成实质审核。

拒绝或返工不会自动重置旧任务、扩大范围或清空重试预算。先处理未结束实例，按实际批准更新计划 revision，再用新任务 ID 登记修复和重新验证；保留旧任务、证据和审核记录用于追溯。0.5.0 的自动集成会考虑当前计划中登记的原始任务；即使已经产生可供下游使用的阶段集成，也不能把仍存在的失败原任务当作完整计划已完成。登记替代项不会自动撤销旧任务的阻断。

## 9. 异常与恢复

重新运行 `run` 会对未结束实例和已完成但未处理的记录进行对账：未领取的 outbox 可以继续处理；已存在启动身份的实例必须先核对；有持久整树退出证明的实例才能释放并处理候选。不能证明退出的实例转为/保留 LOST，继续收费容量并显示阻断。

账本实现任务最多两次自动重试的预算和退避，但当前业务流水线会把未分类的非零退出、失败检查或后处理错误阻断，要求诊断；它不会把潜在外部副作用重复运行。不存在一条通用的“强制清除 LOST”命令。

可先读取 status、事件和对应 attempt 的日志，确定是宿主 CLI 不存在、配置不兼容、权限拒绝、测试失败、范围越界、集成冲突，还是不确定的进程状态。修复原因后走新的批准任务/计划修订或有证据的恢复；直接编辑数据库或删除身份/启动日志会破坏安全判断。

## 10. 安装英文策略、Skill 与宿主包

```bash
parallel-dev policy inspect --project /absolute/path/to/project
parallel-dev policy apply --project /absolute/path/to/project
```

检查后选择 apply。已有 `AGENTS.override.md` 时，明确指定已存在的 override；程序不会静默创建无效优先级的文件：

```bash
parallel-dev policy apply --project /absolute/path/to/project --target AGENTS.override.md
```

移除自己拥有的区块：

```bash
parallel-dev policy remove --project /absolute/path/to/project
```

受管块之外保留原字节、换行、BOM 和用户内容。人工修改受管块、损坏备份、未知标记或未完成注入事务会停止操作，避免误删。

构建宿主包：

```bash
parallel-dev package --adapter codex --source /absolute/path/to/xiaolvji --output /absolute/path/to/new-dist/codex
parallel-dev package --adapter zcode --source /absolute/path/to/xiaolvji --output /absolute/path/to/new-dist/zcode
parallel-dev package --adapter zcode --platform windows --source /absolute/path/to/xiaolvji --output /absolute/path/to/new-dist/zcode-windows
```

`package` 使用完整源码仓库，pip 安装后也应明确给出 `--source`。输出包括真实 Python 程序、英文 Skill/策略、宿主 manifest、Hook、MCP 和摘要清单。它不自动安装、授权或发布。具体宿主加载和信任审查见 [宿主安装指南](HOST-INSTALL.zh-CN.md)。

## 11. MCP 使用边界

```bash
parallel-dev --state-dir /absolute/path/to/controller-state mcp
```

这是 stdin/stdout JSON-RPC 服务，不是供人输入自然语言的终端。它实现初始化握手型 MCP `2025-11-25`。默认 reader 暴露 `parallel_status`、`parallel_events`、`parallel_queue`、`parallel_metrics`、`parallel_project_tree` 和 `parallel_read_file` 六个只读工具；它不派发进程、不批准计划、不提交验证、不完成最终审核。源码接口只读取已登记项目的固定可达提交，不访问未提交文件或任意宿主路径。

可信协调端需要登记任务时，可由宿主的显式 MCP 配置启动：

```bash
parallel-dev --state-dir /absolute/path/to/controller-state --identity-file /absolute/path/to/controller-state/identities/coordinator.key mcp
```

此时才暴露 `parallel_register_task`。凭据路径只放在可信控制器/协调端配置，不能复制进插件包或 Worker 副本。协议客户端传来的 role、producer 或 session_id 不会覆盖经过认证的身份。MCP 提供接口，实际的持续补位仍由 `run` 进程负责。
