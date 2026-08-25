---
title: "单次 DFM 分析数据说明"
status: active
milestone: M2.5-A
last_updated: 2026-08-25
type: living-runbook
owners: DFM 工程团队
---

# 单次 DFM 分析数据说明

本文说明当前开发版本中，一次真实 DFM 分析如何执行、输入和过程数据保存在哪里、结果文件分别有什么用途，以及出现异常时应检查哪些文件。

本文是随 DFM 里程碑持续更新的活文档。这里描述的是**当前已实现行为**；开发阶段参见
[DFM 开发路径](plans/2026-07-13-dfm-hermes-agent-development-roadmap.md)，架构和契约参见
[DFM 架构、工作流与 OCCT C++ 契约](plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md)。

## 1. 当前实现与生产目标

| 能力 | 当前实现 |
| --- | --- |
| 制造工艺 | 注塑 `injection` 完整基线；压铸 `die_casting` 首条 STEP 拓扑有效性门 |
| 三维输入 | 外部 Analysis Situs/OCCT `dfm-geometry` 支持 STEP/STP；Parasolid `x_t` 与 NX 路线已移出当前 intake/执行路径 |
| 2D 图纸/OCR | 接口预留，尚未形成生产分析闭环 |
| 混合输入融合 | 接口预留，尚未形成生产分析闭环 |
| 几何计算 | 独立 Analysis Situs/OCCT C++ 可执行文件，Capability 和算法成熟度为 `experimental`，必须显式选择 `verification_level=experimental` |
| 本体/工艺规则 | Ontology Snapshot Schema 2；注塑 `injection.default@1.1.0` 和本地只读 SQLite；压铸 `die_casting.topology-baseline@1.0.0` |
| 执行方式 | Hermes 主进程管理 Run，并以版本化 JSON/JSONL 协议启动外部 `dfm-geometry analyze` 子进程 |
| 结果 | C++ Engine `preflight/topology/render_mesh/features/measurements/metric_fields/engine_result` JSON；Hermes `evaluations.json`、报告及可生成的证据制品 |
| Desktop | 复用附件上传、聊天进度和 Artifacts 展示 |

外部 C++ 项目已经通过本地 CLI 契约接入，但当前只证明实验级 Objective Calculation，不等于
生产认证。Hermes 的 `discover` 目前冻结可审计的 ordinary 全模型区域；外部两阶段工艺特征
Recognizer 尚未接入该调用链，因此不得把 fallback 表述为真实识别结果。PythonOCC、NX 与
Parasolid 均不在当前执行或静默降级链路中。

当前版本不分析模具设计模型，也不分析型芯、型腔、滑块、顶针、浇注系统或冷却系统；
压铸尚未开放壁厚、拔模和倒扣规则。

## 2. 一次分析的调用流程

```text
用户 / Desktop
  │
  ├─ 上传 STEP/STP
  │
  v
Hermes Agent
  │  理解目标、选择 injection/die_casting、必要时追问
  │
  ├─ dfm_project(create)
  ├─ dfm_project(add_input)
  ├─ dfm_project(confirm_fact)      # 回答 discovery 阶段澄清
  ├─ dfm_analysis(discover)         # 必须先冻结 DiscoverySnapshot
  ├─ dfm_project(confirm_fact)      # 回答 analysis 阶段澄清
  ├─ dfm_analysis(context)          # 可选；按需读取单个 Check 的本体上下文
  ├─ dfm_analysis(plan)             # 编译 analysis Plan
  ├─ dfm_analysis(start)
  ├─ dfm_analysis(status)           # 轮询/进度
  └─ dfm_analysis(result)
          │
          v
DFMService
  ├─ DiscoveryEngine → ordinary whole-model fallback（当前）
  ├─ Local Ontology SQLite + ProcessAdapter → AnalysisPlan
  └─ JobManager → Analysis Situs/OCCT dfm-geometry → Objective artifacts
                    └─ Hermes Evaluation → Evidence → Finding → JSON/MD/PPTX
```

### 2.1 Agent 与确定性计划的分工

- Hermes Agent 负责理解用户意图、选择工艺、补充或确认工程事实，并决定何时调用 DFM 工具。
- `DFMService` 不直接执行模型临时生成的几何步骤。它将本地已发布本体/规则快照与几何 Capability
  组合，根据已确认事实编译结构化 Plan。
- `discover` 必须先于 analysis `plan`。当前 Discovery 只产生可审计的 ordinary 全模型区域和
  外部 OCCT Provider 的显式未实现状态，不伪造螺钉柱、筋等工艺特征。
- `dfm_analysis(context)` 按 Check 返回概念定义、Operand、Factor、选项和候选规则，使 Agent/AI
  实际消费本体；它不把完整数据库放入模型上下文。
- Run 启动前会保存 Plan 快照；worker 只执行该快照对应的参数和操作。
- Analysis Situs/OCCT 测量值和 Hermes 规则判断均由确定性代码产生，不由大模型编造；当前
  Objective Calculation 已由独立 C++ 项目执行，外部 Geometry Discovery/Recognizer 调用仍待接入。

## 3. 数据根目录与标识

DFM 工作区跟随当前 Hermes profile：

```text
<HERMES_HOME>/workspace/dfm/
```

Windows 默认 profile 通常为：

```text
C:\Users\<用户名>\.hermes\workspace\dfm\
```

Docker 中通常通过 `HERMES_HOME` 指向持久卷，例如：

```text
/data/hermes/workspace/dfm/
```

一次分析涉及三个主要标识：

| 标识 | 示例 | 作用 |
| --- | --- | --- |
| `project_id` | `dfm_bcd8dc5bac814f30` | 一个可持续追加输入、事实、Plan 和 Run 的 DFM 项目 |
| `plan_id` | `plan_4c61...` | 一份已持久化的分析计划 |
| `run_id` | `run_28d9bfd0564e4f1e` | 对某个 Plan 的一次实际执行 |

同一项目可以有多个 Plan 和多个 Run。诊断时必须同时确认 `project_id` 与 `run_id`，不能只看聊天会话 ID。

## 4. 当前真实目录结构

```text
<HERMES_HOME>/workspace/dfm/
├── ontology/
│   └── dfm-ontology.sqlite3
├── projects/
│   └── <project_id>/
│       ├── project_manifest.json
│       ├── inputs/
│       │   └── input_<sha256前16位>.stp
│       ├── runs/
│       │   └── <run_id>/
│       │       ├── request.json
│       │       ├── events.jsonl
│       │       ├── worker.stdout.log
│       │       ├── worker.stderr.log
│       │       └── artifacts/
│       │           ├── preflight.json
│       │           ├── topology.json
│       │           ├── render_mesh.json
│       │           ├── features.json
│       │           ├── measurements.json
│       │           ├── metric_fields.json
│       │           ├── engine_result.json
│       │           ├── evaluations.json
│       │           ├── evidence_geometry.json
│       │           ├── evidence_records.json
│       │           ├── evidence_<序号>.png     # 仅测量提供可验证局部场时
│       │           ├── dfm_report.json
│       │           ├── dfm_report.md
│       │           └── dfm_report.pptx       # 安装 python-pptx 时
│       ├── artifacts/
│       └── reports/
├── tmp/
└── .locks/
```

当前 STEP Analyzer 将本次运行的结果写入 `runs/<run_id>/artifacts/`。项目根目录下的 `artifacts/`
和 `reports/` 是预留目录，不是当前 STEP 结果的主要读取位置。目录中的具体 ScalarField 数量和
证据图片数量由 Plan、失败 Evaluation 和配置决定，不能依赖固定文件个数。

## 5. 输入数据

### 5.1 Desktop 附件

Desktop 上传或选择的文件只是 intake 来源，不是 DFM 项目的权威输入。Agent 调用 `dfm_project(add_input)` 后，DFM 才会登记该文件。

### 5.2 项目输入副本

登记 STEP 时会：

1. 检查扩展名和文件大小；
2. 流式计算 SHA-256；
3. 复制到项目 `inputs/`；
4. 校验 ISO 10303-21 格式、B-Rep 声明并记录实体复杂度摘要；
5. 以内容哈希命名；
6. 将 InputRecord 写入 `project_manifest.json`。预检失败不会保留项目输入副本。

InputRecord 主要字段：

```json
{
  "input_id": "input_step_<sha256前16位>",
  "kind": "step",
  "source_name": "用户上传文件名.stp",
  "relative_path": "inputs/input_<sha256前16位>.stp",
  "size_bytes": 123456,
  "sha256": "...",
  "created_at": "...",
  "preflight": {
    "status": "passed",
    "format": "iso-10303-21",
    "brep_representation": "declared",
    "complexity": {"entity_count": 1234}
  }
}
```

STEP 项目按阶段确认事实：`model_units` 属于当前 Discovery 前置事实；`material` 和 `pull_dir`
属于当前注塑 Analysis 前置事实。未确认项以稳定 clarification ID 写入 Manifest；`confirm_fact`
只保存用户明确回答并关闭对应问题。`plan` 在没有有效 DiscoverySnapshot 时返回
`discovery_required`，不会跳过发现阶段。新增输入或确认影响既有计划的事实会把相关 Plan 标记为
`invalidated`，需要重新发现或重新规划。

同名同类型的新输入会以 `supersedes_input_id` 指向旧版本；后续 Plan 仅引用未被替代的活动输入。失效 Plan 会保存 `invalidated_by` 和 `affected_operation_ids`。调用 `dfm_analysis(plan, base_plan_id=...)` 可以从失效 Plan 生成仅包含受影响检查及其依赖的重跑 Plan；例如当前 ontology 只发布主体壁厚与拔模角 Check，仅修改拔模方向时重跑范围为几何预检、拓扑/AAG 与拔模测量，而不是完整操作族。

相同类型且哈希相同的输入会复用既有记录。分析追溯以项目输入副本和哈希为准，不依赖原始附件路径持续存在。

## 6. 项目权威数据：project_manifest.json

`project_manifest.json` 是项目事实的权威来源，聊天记录不是项目数据库。Manifest 当前包含：

- 项目名称、版本和更新时间；
- 输入列表及哈希；
- 用户确认的工程事实；
- Plan 列表；
- Run 列表和状态；
- Run 对应的 artifact 引用；
- 已声明的能力状态。

每次写入会增加 `revision`，并通过锁和原子替换降低并发写坏风险。

### 当前边界

`facts`、`clarifications`、`observations`、`features`、`regions`、`discovery_snapshots` 和 `findings`
契约已经存在。当前实现将失败 Evaluation 归一化为带规则引用的项目级 Finding：

- 已确认工艺参数可以写入 `facts` 并参与 Plan 编译；
- 每次 STEP Run 都生成 `measurements.json`，保存输入哈希、算法版本、实际 operations 和客观模型测量；规则比较结果由 Hermes 单独写入 `evaluations.json`；
- 原始兼容问题仍保存在 `dfm_report.json` 和最终报告中，旧报告格式没有被改写；
- Finding ID 由输入哈希和稳定 Evaluation ID 派生，包含版本化 rule 引用，并引用测量、报告及同次运行的证据制品；
- `ProjectManifest.findings` 是项目级风险浏览入口，精确测量仍以被引用的 `measurements.json` 为准。

## 7. 分析计划与 worker 请求

### 7.1 Manifest 中的 PlanRecord

PlanRecord 保存：

- `phase`：`discovery` 或 `analysis`；只有 analysis Plan 可以启动客观计算 Run；
- `process`：由 Plan 固定为 `injection` 或 `die_casting`；
- `process_adapter_version`；
- `scope_id` 与 `scope_version`；
- `ontology_snapshot_id` 与 `ontology_snapshot_sha256`；
- `discovery_snapshot_refs`；
- 输入 ID 和输入哈希；
- 版本化 Operations、Region 和每个参数的值、单位、来源；
- Effective Rules、RuleBindings 和多 Measurement Operand 表达式。

它回答“准备分析什么、使用哪些输入、参数从哪里来、采用哪版规则范围”。

### 7.2 Run 中的 plan_snapshot

启动 Run 时会把完整 Plan 保存为 `plan_snapshot`。即使项目后来增加新事实或新 Plan，既有 Run 仍能回溯当时实际执行的计划。

### 7.3 request.json

`runs/<run_id>/request.json` 是 `OcctAnalyzer` 发给外部 `dfm-geometry` 的请求，主要包含：

- worker schema 版本；
- `run_id`；
- 项目输入文件绝对路径；
- 本次 artifact 输出目录；
- 工艺、范围和分析器版本；
- 外部协议允许的 Objective Operations、已解析参数和实验验证级别。

规则条件、阈值、pass/fail、Fact 依赖和 Feature/Region 规划元数据不发送给几何引擎；它们留在
Hermes 的 Plan/Evaluation 阶段。当前外部 Objective v2 只消费客观 Calculator 输入。

`request.json` 是复核“worker 实际收到了什么”的首选文件，但其中的绝对路径属于运行环境路径，迁移到另一台机器后不应直接复用。

## 8. 运行过程数据

### 8.1 Run 状态

```text
queued ──> running ──> succeeded
   │          ├──────> failed
   │          ├──────> cancelled
   │          └──────> blocked
   ├─────────────────> failed
   ├─────────────────> cancelled
   └─────────────────> blocked
```

RunRecord 同时保存：

- analyzer 名称和版本；
- Plan ID 与 Plan 快照；
- stage 和 progress percent；
- heartbeat；
- owner PID 与 runtime ID；
- error；
- artifact 列表；
- 三个诊断日志的相对路径。

### 8.2 events.jsonl

`events.jsonl` 每行是一个 UTF-8 JSON 对象，用于记录 worker 的结构化事件，例如：

- `progress`：阶段与百分比；
- `artifact`：新制品名称和类型；
- `error`：结构化错误码和消息；
- `completed`：worker 结果文件。

它适合时间线分析和 UI 进度恢复，不应把普通 stdout 文本当作权威状态。当前 WorkerEvent
Schema 没有独立 `heartbeat` 事件；运行存活时间记录在 RunRecord 的 `heartbeat_at`，由进度、
Artifact 和 Hermes 阶段更新推进。

### 8.3 worker.stdout.log

保存外部引擎标准输出。当前协议要求每个非空行都是纯 JSON WorkerEvent，不允许前缀或普通日志
混入 stdout。主要用于：

- 检查事件是否实际发出；
- 定位进度停在哪个阶段；
- 排查事件解析或编码问题。

### 8.4 worker.stderr.log

保存警告和异常堆栈，主要用于排查：

- STEP/B-Rep 读取失败；
- OpenCascade 几何计算异常；
- 渲染或证据图片失败；
- PPTX 报告生成失败；
- 子进程依赖、编码或退出异常。

## 9. 分析结果数据

| 文件 | 类型/用途 | 主要使用者 |
| --- | --- | --- |
| `engine_result.json` | 外部 Objective Result Manifest：输入/范围、Producer 版本、协议版本和 Artifact 元数据 | Analyzer、开发诊断 |
| `preflight.json` | STEP 单位、B-Rep 有效性及可审计 Healing 诊断 | 输入质量与几何归一化审计 |
| `topology.json` | 稳定拓扑映射身份 | 几何引用与契约校验 |
| `render_mesh.json` | 外部引擎三角网格及质量信息 | 可视化/后续证据适配 |
| `features.json` | 当前执行操作产生的客观识别记录；不替代 Hermes DiscoverySnapshot | 开发诊断、后续发现适配 |
| `measurements.json` | 几何 Worker 输出的版本化客观 Measurement、Operation 引用和几何引用 | EvaluationEngine 输入、系统集成、开发诊断 |
| `metric_fields.json` | 外部引擎汇总的 ScalarField 与可审计视图引用 | Measurement 场引用校验、证据适配、开发诊断 |
| `evaluations.json` | Hermes EvaluationEngine 使用 Plan 参数/版本化规则比较后生成的 Evaluation 和 provenance | Finding 归一化、规则审计 |
| `evidence_geometry.json` | 失败 Evaluation 对应的 FailedPatch 几何 | 证据审计 |
| `evidence_records.json` | Evaluation、Measurement、Region 与图片的结构化关系 | 报告、Finding |
| `evidence_*.png` | 当前失败区域证据图 | 问题详情、PPTX |
| `dfm_report.json` | 汇总 Measurement、Evaluation 和 Evidence 的结构化 DFM 结果 | Desktop、系统集成 |
| `dfm_report.md` | 可读文本报告和兼容交付 | Agent、开发者 |
| `dfm_report.pptx` | 安装 `python-pptx` 时生成的演示交付报告 | Desktop 用户 |

当前只有失败 Evaluation 能关联有效 ScalarField/RenderScene 适配制品时才生成局部证据图片；
外部引擎只返回 `render_mesh` 并不自动等于已经满足该证据契约。每个入选 FailedPatch
最多生成三个自适应视角：

- 出模方向视图 `pull`，未提供方向时为 `overview`；
- 局部表面法向视图 `surface`；
- 正交侧视图 `side`。

`dfm.evidence.max_rendered_findings` 当前实际限制一次 Run 最多生成的证据图片总数；并非所有失败
都会生成三张图片。当前确定性证据管线不生成 `dfm_highlighted.step`、`model.png`、`overview.png`
或旧式 `DFM-*.png`，这些名称不得作为集成契约。

## 10. 数据追溯关系

```text
项目输入文件
  └─ SHA-256 / InputRecord
       └─ DiscoverySnapshot → Feature / Region
            └─ Ontology Snapshot + RuleBinding + AnalysisPlan
                 └─ RunRecord.plan_snapshot
                      └─ request.json → engine_result.json / measurements.json / metric_fields.json
                           └─ evaluations.json → evidence → Finding / Report
                                └─ ArtifactRecord.relative_path + SHA256
```

复核一个问题时，推荐顺序为：

1. 从 PPTX、`dfm_report.json` 或 Manifest Finding 找到 Evaluation ID；
2. 在 `evidence_records.json` 找到证据图片、Measurement 和 Region 引用；
3. 在 `evaluations.json` 核对规则版本、表达式、Operand 值和结果；
4. 在 `measurements.json` 与 `metric_fields.json` 核对客观测量、场定义和可审计视图；
5. 查看 `request.json`、DiscoverySnapshot 与 Plan 固定的本体快照 ID/哈希；
6. 查看输入哈希和实现版本，必要时使用项目 `inputs/` 中的 STEP 复算。

## 11. 如何找到最近一次分析

PowerShell 示例：

```powershell
$dfmRoot = Join-Path $env:USERPROFILE ".hermes\workspace\dfm"

# 最近更新的项目
Get-ChildItem "$dfmRoot\projects" -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 5 FullName, LastWriteTime

# 某项目最近的 Run
Get-ChildItem "<项目目录>\runs" -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object FullName, LastWriteTime

# 查看结构化事件
Get-Content "<Run目录>\events.jsonl" -Encoding UTF8

# 查看错误日志
Get-Content "<Run目录>\worker.stderr.log" -Tail 200 -Encoding UTF8

# 查看结果文件
Get-ChildItem "<Run目录>\artifacts" -File |
  Select-Object Name, Length, LastWriteTime
```

环境和能力自检：

```powershell
python .\hermes dfm doctor --json
```

`dfm doctor` 只验证配置、工作区、外部几何可执行文件/协议、ProcessAdapter、默认 ontology
快照和可选报告依赖，不代表某个具体 STEP 已经分析成功或算法已经获得生产认证。

## 12. 成功、失败和中断的判断

### 成功

- Manifest 中对应 Run 为 `succeeded`；
- `events.jsonl` 有且仅有一个有效 completion 结果；
- `engine_result.json` 可解析且协议身份匹配；
- Run artifact 已登记并且文件存在；
- JSON/PPTX 能打开，证据引用与图片对应。

### 失败

- Run 为 `failed`；
- RunRecord.error 包含结构化错误；
- 优先查看 `worker.stderr.log`，再结合 `events.jsonl` 和 stdout；
- 如果 worker 已生成部分文件但未登记为 artifact，不应把这些文件视为正式交付结果。

### 取消

- Run 为 `cancelled`；
- 已登记的诊断数据可以保留；
- 部分生成的报告或图片不代表完整分析。

### 阻塞

- Run 为 `blocked`；
- 常见原因包括能力未实现、依赖缺失、Plan 不可执行或输入条件不足；
- 应补充条件或恢复依赖后创建新 Plan/Run，不直接篡改旧 Run。

## 13. 保留、清理与安全

- `project_manifest.json`、`inputs/`、`runs/` 和已登记 artifact 是可审计数据，不应随聊天清空。
- `tmp/`、锁和未登记的临时文件可以按清理策略处理。
- `keep_failed_runs: true` 时保留失败 Run，便于定位 OCC 和报告问题。
- STEP、报告和证据图片可能包含产品知识产权，生产环境应限制工作区访问权限并设置备份、保留和安全删除策略。
- Manifest 保存 canonical 相对路径；工具返回给 Desktop 时可以附加当前环境的绝对路径。不要把绝对路径当成跨机器稳定标识。
- 不要手工编辑运行中的 Manifest、`events.jsonl` 或 artifact。需要更正事实时创建新事实、Plan 或 Run。

## 14. 活文档更新规则

以下变化必须与代码在同一个变更中更新本文：

| 变化 | 必须更新的章节 |
| --- | --- |
| 新增输入类型或制造工艺 | M1 适用范围、输入数据、调用流程 |
| 修改工作区或 artifact 路径 | 数据根目录、真实目录结构、排查命令 |
| 修改 Manifest/Plan/Run/worker schema | 对应数据说明和追溯关系 |
| 新增/删除报告或证据文件 | 分析结果数据 |
| 修改状态机、进度或取消语义 | 运行过程、成功失败判断 |
| 完成 Finding/Measurement 归一化 | Manifest M1 边界、结果读取优先级 |
| 进入新里程碑 | front matter 的 `milestone`、能力矩阵和文档日期 |
| 本体发布 Schema 或默认 Snapshot 升级 | 能力矩阵、Plan 字段、追溯关系和相关文档 |

更新时遵守：

1. 当前事实与未来计划分开写；
2. 目录结构以真实代码和一次 E2E Run 为准；
3. 示例只使用合成或脱敏数据；
4. 不冻结容易变化的 issue 数量；
5. 修改后至少用一个真实 Run 核对文件名、路径、状态和 artifact 登记。

## 15. 相关文档

- [DFM Hermes Agent 开发目标与路线图](plans/2026-07-13-dfm-hermes-agent-development-roadmap.md)
- [DFM 架构、工作流与 OCCT C++ 契约](plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md)
- [DFM 本体、规则库与 Agent 运行快照设计](dfm-rule-catalog-database-design.md)
- [DFM 部署环境定义](dfm-deployment-environment.md)
