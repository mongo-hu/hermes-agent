# Hermes：Geometric 契约接入与模型会话管理

更新日期：2026-10-21。状态：已落地；Hermes 常驻客户端、增量编排与 Worker 公共 v2 Schema 已完成联调。

[开发导航](README.md)

总体流程见[两轮分组确认的DFM主流程](dfm-main-workflow-explained.md)。本方案规定 Hermes 如何调用常驻 Worker、管理项目与快照、组织技术指标计算，以及消费结果和生成报告。

## 1. Hermes 的职责与改造范围

Hermes 负责决定“检查什么、需要算什么、结果是否符合要求”；OCCT Worker 负责识别结构和计算客观几何量。

| Hermes 负责 | 交给 Worker |
| --- | --- |
| 保存上传模型，关联聊天和 DFM 项目 | 加载 STEP，持有实际几何对象 |
| 查询 Feature → Check → Geometric 和规则 | 识别特征，保存内部部位和几何关联 |
| 确认材料、方向等必要输入，固定分析计划 | 按特征和 G ID 选择部位、执行算法 |
| 去重计算需求，管理进度、取消和恢复 | 复用模型与计算资源，逐 G 返回结果 |
| 执行规则，发布问题、定位和 HTML 报告 | 提供数值、测量点、面引用及采样数据 |

`occt.py` 与 `feature_recognition/occt_cpp.py` 现通过项目归属的常驻会话共用 Worker、模型快照和恢复记录；预览、识别、修订与测量不再各自启动短命分析进程。

开发阶段直接切换新契约，不增加旧协议适配器、字段别名或静默 fallback。不修改 Mold 数据库，不自动删除或转换已有项目和运行产物。

## 2. 共同契约与 Hermes 的使用方式

### 2.1 文档和接口如何维护

两仓库的文档按职责分别编写，不要求全文相同：

| 内容 | 维护位置 | Hermes 的要求 |
| --- | --- | --- |
| G ID 的工程含义、适用部位与取值方法 | 本体规则库 | 使用发布的权威 G ID，不另建 Worker 专用 ID |
| 方法、字段、状态和消息结构 | Worker 仓库 `docs/dfm-geometric-session-contract.md` 第 2 节 | 按同一约定构造请求、校验结果 |
| 可执行契约和联调样例 | Worker 仓库 `schemas/` 及契约测试样例统一维护并随版本发布 | 固定所接入的契约制品版本，运行消费端契约测试 |
| 客户端、计划、规则、Viewer 和报告接入 | 当前 Hermes 文档 | 由 Hermes 实现和验证 |

主契约为 `dfm.geometry/v2`。Worker 仓库发布的 `schemas/` 是可执行契约的唯一来源，Hermes 固定并校验 `2.0.0` Schema bundle；双方确认公共接口变更后更新同一套定义和样例，不在 Hermes 手工维护另一套近似协议。

契约版本用于校验通信格式；引擎构建及依赖指纹用于追溯与缓存。不能只检查 EXE 版本号，也不能以两篇说明文档的内容哈希相同作为兼容证明。

### 2.2 Hermes 保留哪些标识

| 标识 | 在 Hermes 中的用途 |
| --- | --- |
| `project_id` / `run_id` | 项目与一次分析的记录，不作为 Worker 的计算对象编号 |
| `check_id` | 业务指标，如带孔 BOSS 柱“柱壁”；留在规则与问题处理层 |
| `snapshot_id` | Worker 返回的模型及特征状态编号，用于后续识别、修订和计算 |
| `feature_id` / `feature_kind` | 快照内的具体结构及其类别，例如 `BOSS_0007` / `hollow_boss` |
| `geometric_id` | 两端统一的技术指标 G ID |
| `forming_scope_id` | 快照内的运行时成型范围，如MAIN_POS或SIDE_01；不新增本体Region概念 |
| `context` | 该G实际需要的确认输入和算法参数；方向相关项可绑定forming_scope_id与显式用户方向 |
| `job_id` | Worker 一批几何计算的进度、查询和取消编号 |
| `evidence` | 数值的几何依据，包括面、测量点、局部采样数据和对应快照身份 |

普通计算不再使用 `worker_geometric_id`、`operation_id`、`calculator_id`、`metric_id/metric_ids`、`quantity_id` 路由，也不要求构造 Region 或 `face_indices`。传输层请求 ID 仍用于匹配应答，不再为每个 G 增加 `task_id`。

本体通用表中的 `concept_id` 仍保留，值为该技术指标的 G ID；Worker 的技术指标接口称为 `geometric_id`。规则包的 `properties_json`、`qualifiers_json`、`conditions_json`、`acceptance_criteria_json` 沿用数据库设计中的名称。

### 2.3 Hermes 何时调用各接口

| 方法 | 调用时机 | Hermes 保存或处理什么 |
| --- | --- | --- |
| `capabilities.get` | 连接建立后 | 契约版本、G/Feature、scope_modes、direction_modes、requires_complete_inventory与输入要求 |
| `snapshots.open` | 没有有效模型快照时 | 新 `snapshot_id`、导入诊断、预览与拓扑映射引用 |
| `features.discover` | 按feature_kinds与可选forming scope分批识别；长任务跟踪job_id | base/结果快照、实例、关联及coverage |
| `features.revise` | 用户修正特征部位时 | 新快照及修订记录，不覆盖旧 Run 使用的版本 |
| `forming_scopes.analyze` | 基于主/局部方向分析成型范围、可见性与倒扣候选；长任务返回job_id | base/结果快照、候选范围、覆盖和诊断 |
| `forming_scopes.revise` | 保存用户修订的范围、成型侧和局部方向，校验引用与一致性 | 新快照及修订后的成型范围 |
| `snapshots.merge` | 校验共同base、读依赖和写冲突后合并派生结果 | 新快照，冲突返回snapshot_conflict |
| `geometrics.compute` | 计划中仍有未取得的 G 结果 | Worker `job_id` 与本地 Run/JobManager 任务的关联 |
| `jobs.status` / `jobs.cancel` | 查询或取消几何计算批次 | 任务状态；取消不等于强制结束共享 Worker |
| `snapshots.renew` / `snapshots.close` | 使用期间续租、无使用者时释放 | 快照有效性和项目当前引用 |

## 3. 常驻客户端：把多次 EXE 调用改成持续连接

Hermes 管理 `dfm-geometry serve --stdio` 子进程，通过单行 JSON-RPC 2.0 通信。预览、Discovery 和正式计算共用连接，不各自创建短命分析进程。

客户端需实现：

1. 启动、能力握手和契约校验；缺少 v2 能力时明确报错。
2. 持续读取应答和任务事件，独立收集 stderr 日志；不得将日志当成协议数据。
3. 按请求 ID 分发应答，按 `job_id` 更新 JobManager 的状态和进度。
4. 几何计算期间仍能发送续租、查询和取消消息，不能让等待结果阻塞整个连接。
5. 连接中断或进程退出时，将所属运行句柄标为失效并通知相关任务。

多个项目不能共用一个无归属的“当前模型”变量。Hermes 保存项目、快照及所属 Worker 连接的关联；同一模型的计算由 Worker 排队，多进程调度时请求必须发往持有该快照的进程。

会话超时、内存预算和并发参数使用 `config.yaml`，状态与缓存遵守 Hermes profile 隔离。外部 C++ OCCT 与内部 PythonOCC 保持明确的后端选择，不因接口缺失而悄悄切换。

## 4. 两轮分组确认与增量快照

### 4.1 确认按范围解锁任务

用户上传模型后，加载/预览与主方向确认同时推进。模型就绪后，一支做尽量不依赖方向的结构候选识别，另一支在主方向确认后做可见性、成型侧和倒扣候选分析。外观面仅提出几何候选，由用户确认用途。

| 确认组 | 用户确认内容 | 解锁范围 |
| --- | --- | --- |
| 第一轮：主体壁与普通结构 | 主体壁、筋、凸台、带孔 BOSS 柱、孔及部位/关联 | 相关壁厚、尺寸、圆角、净距和关系指标 |
| 第一轮：产品与成型属性 | 外观范围/等级、型芯/型腔侧、边界及主方向约定 | 规则选择、相应主方向拔模和方向评价 |
| 第一轮：倒扣候选范围 | 各候选的范围，可拆分/合并，暂不必给出所有局部方向 | 局部结构关联与补识别，方向无关测量 |
| 第二轮：各局部组 | 局部退出方向、作用范围、区域内结构与参考主壁 | 该组的局部拔模与方向评价 |

每组独立提交，高置信对象可由用户批量确认，歧义项单独处理。确认 U01 不等待 U02；主体壁确认后不等待所有局部方向。未分类范围保留基础检查和覆盖记录。

局部结构确认和局部方向确认分别记录。目标部位已确认即可运行不依赖方向的G；候选未确认时只可做可丢弃的预计算，不发布正式合格结论。

Hermes 为确认组保存内部 `review_group_id`、目标对象/范围、所审内容指纹、用户来源、状态及时间。确认只对当时内容有效，相关部位或方向变化时重置受影响确认项；审核用途与清单完整性不能仅凭识别 confidence 自动通过。

### 4.2 识别结果与成型范围

Worker 分别保存几何 Feature、面级产品属性、运行时成型范围。`forming_scope_id` 只标识某快照内的方向作用域，不新增本体 Region 或业务 Check。

`features.discover` 按 `feature_kinds` 分批调用；局部请求通过 `context.forming_scope_id` 指定补识别范围。已识别的孔、筋、柱沿用实例编号，局部阶段只补关联或遗漏结构；目标范围外的主壁、支撑和遮挡仍需完整模型查询。

Hermes 读取 `coverage` 中的 `feature_kind/context/status/covered_geometry_ref/unresolved_geometry_ref/diagnostics`。状态为 `complete/partial/failed`，用于判断搜索是否完整；未完成筋清单不能让支撑数量返回 0，未完成邻域对象清单不能发布最终最近距离。complete 不等于识别已获用户确认。

局部方向和范围通过 `forming_scopes.revise` 写入新快照。向量采用模型坐标，表示成型部件相对塑件退出方向；Hermes 保存原输入及变换来源，不用柱轴或包围盒方向替代。主方向下的倒扣与局部退出评价分别保留。

### 4.3 派生快照合并与不可变批次

```text
snapshot_A0：同一个已加载模型
  ├─ 结构识别 → snapshot_A_structure
  └─ 主方向分析 → snapshot_A_direction
             ↓ snapshots.merge：共同 base、读依赖与写冲突校验
       snapshot_A1：结构 + 成型候选
             ↓ 分组修订/确认
       snapshot_A2：普通区域可计算，SIDE_01/SIDE_02 可分别推进
```

每个耗时写任务返回 `base_snapshot_id` 与结果 `snapshot_id`。Hermes 用 `snapshots.merge` 合并无冲突结果；`snapshot_conflict` 时保留两份产物并重跑相关分支，不用最后返回的快照覆盖项目当前状态。

输入副本、识别结果、审核修订、网格和计算产物保留不可变版本。快照中不同状态共享 LoadedModel；仅审核而未改变内容时可沿用快照，由 Hermes 保留确认记录。CAD 几何变化则重新登记模型。

### 4.4 聊天、续租与恢复

原始 STEP 保存到项目并记录哈希、单位和导入参数，不依赖聊天临时路径。Hermes 保存项目—Worker 连接—快照关联。用户审核、等待事实或计算期间续租；关闭聊天只释放对应引用，活动任务仍持有其所需快照。

快照过期或 Worker 重启后，重新打开输入，恢复并校验特征、属性、成型范围和用户方向，取得新句柄。旧面号不直接套用；无法验证的修订重新审核。磁盘 JSON 是恢复材料，不证明旧内存仍在。

## 5. 分阶段计划与任务依赖

### 5.1 一个分支就绪，就固定一个计划片段

Run 固定原始模型和本体规则发布版本，执行期间可以追加不可变的 `plan_fragments`。每个片段固定自己的几何/特征快照、已确认事实、适用规则、G/context、依赖指纹和能力版本；Worker 批次不接收计划片段 ID 或业务规则。

| Run 内记录 | 用途 |
| --- | --- |
| `plan_fragments` | 各已就绪分支的不可变计划；内部片段编号用于进度和追溯，不是每 G 的 task_id |
| 片段 `supersedes` 关联 | 修订后追加替代片段，旧片段和测量保持原状 |
| 结果有效性记录 | 当前结论仍有效或已 stale，保留失效原因与来源事件 |
| `result_manifest` | 生成某版报告时固定采用的片段、快照、结果绑定、规则、网格、证据和未完成项 |

新确认先追加片段，不修改已派发任务的输入。相关依赖改变时撤下过期结论、追加替代计划；原始结果和历史事件不改写。Run 封存后继续修改则新建 Run。规则发布版本变化进入新 Run，不能混入同一报告。

一个 Run 可引用多个来源快照；最终报告必须核验所有选中结果与最终确认状态一致。跨快照复用保留来源、建立已验证的新结果绑定，不把旧结果的 snapshot_id 改成最新值。

### 5.2 从 Check 生成计算需求

```text
某组 Feature、部位与必要属性确认
  → Feature → Check → USES_OPERAND
  → conditions_json 选择规则，补齐相关 Fact
  → acceptance_criteria_json 取得判定要求
  → 检查能力、确认、方向、对象清单等依赖
  → 固定就绪片段，合并同条件 G 需求并提交
```

柱壁检查的变量绑定不变：`boss_wall_thickness` 对应 `G_HOLLOW_BOSS_WALL_THK_MAX`，`adjacent_main_wall_thickness` 对应 `G_HOLLOW_BOSS_WALL_REF_THK`。两个厚度可各自计算，Check 等二者可用再判断；业务比值留在 Hermes。

Check实例同时绑定Feature、Check、适用成型context与规则版本，避免同一结构的主方向评价被局部结果覆盖。方向无关G可共用空context，但业务评价仍保留其实际适用范围。带孔 BOSS 柱优先使用具体子型规则，同一问题不重复报告普通BOSS结果。

同一 G、特征部位、参考对象和有效参数的请求只执行一次，可供多个 Check 使用。不同参考厚度 G 不因数值相同合并；G 的最大/最小口径不由 Check 临时改变。Operand 仍是变量绑定，不新增 CheckOperand 表。

### 5.3 确认、计算和覆盖三类状态

| 状态层 | 状态/含义 |
| --- | --- |
| Hermes 分组/任务 | `waiting_input/pending_review/ready/running/completed/needs_attention/stale/cancelled` |
| Worker 识别覆盖 | `complete/partial/failed`，另保留具体目标、有效范围和未处理范围 |
| Worker 逐 G 结果 | `ok/unsupported/not_applicable/missing_context/ambiguous_scope/failed` |

前置确认或计算缺失时任务不占执行线程，前置失败只阻塞其下游。Check 的可判定条件具备即执行，不等待整批指标和所有区域；规则组合求值按本体规则契约处理。

用户当前区域、确认后的必要补识别优先；其它已确认分支正常执行；精细场、全域加密及 HTML 使用后台预算。JobManager 同时映射本地片段与 Worker job，逻辑上独立推进不等于同时并发访问同一 OCCT 上下文。

## 6. 计算请求、局部方向与结果复用

### 6.1 指标按真实依赖解锁

| 指标 | 必须满足的前置条件 |
| --- | --- |
| 主体壁/柱壁/筋厚 | 目标部位、有效几何及必要配对确认；通常不依赖局部方向 |
| 孔深/孔径 | 孔子型、轴线、开口与终端定义明确 |
| 根部圆角 | 过渡面与根部归属明确；R/t 另等参考壁厚 |
| 支撑筋数量/连接 | 父柱和支撑关系明确，相关筋清单搜索完整且已确认 |
| 最近孔/筋距离 | 目标搜索域对象清单完整、有效配对明确 |
| 主方向拔模/倒扣 | 主方向与对应部位/成型侧明确；不等局部退出方向 |
| 局部拔模/方向评价 | 对应 forming scope、结构和用户局部方向确认，且 G 允许该模式 |

### 6.2 请求与 context

```json
{
  "snapshot_id": "snapshot_A2",
  "items": [
    {
      "feature_id": "BOSS_0007",
      "geometric_ids": [
        "G_HOLLOW_BOSS_WALL_THK_MAX",
        "G_HOLLOW_BOSS_WALL_REF_THK"
      ],
      "context": {}
    }
  ]
}
```

局部方向相关 G 使用以下 context：

```json
{
  "forming_scope_id": "SIDE_01",
  "draw_direction": [1, 0, 0],
  "coordinate_system": "model",
  "direction_source": "user_confirmed"
}
```

`forming_scope_id`、方向和作用面须与输入快照一致。严格限定主方向的 G 不可直接套局部方向；只有定义及 `scope_modes/direction_modes` 明确支持才派发分域请求。G 的有效 context 按声明归一化，不把主方向、材料等无关字段塞入所有厚度缓存键。

### 6.3 接收与复用结果

Hermes 按快照、Feature、G 和有效 context 关联结果，不依赖数组顺序。校验状态、类型、单位、几何身份、路径、清单大小与内容哈希；批次完成不等于所有 G 成功。非 ok 项保留 null 和原因，不填 0。

逐 G 的 `provenance` 记录 `source_snapshot_id/source_result_ref/dependency_fingerprint`。Hermes 直接复用已验证的同快照绑定；跨快照由 Worker 根据实际依赖生成新绑定并保留来源，不能仅因快照变化全量重算，也不能仅因 Feature 编号相同复用。

| 修改 | 保留 | 重新验证/执行 |
| --- | --- | --- |
| 主方向 | 方向无关结构、尺寸、厚度与 R | 主方向区域、相关成型归属、角度和倒扣及下游 |
| SIDE_01 局部方向 | 普通区域、SIDE_02、方向无关测量 | SIDE_01 的方向结果及受影响归属 |
| 外观/纹理标签 | 原始几何采样 | 相关规则、分区统计与判定 |
| 某柱部位或参考主壁 | 无关 Feature 和模型基础资源 | 该柱相关 G、配对和下游 |
| 业务阈值 | 定义与精度仍适用的客观量 | Evaluation、违规定位和报告 |
| 显示网格 | 仍有效的客观几何数据 | 投影、高亮和显示制品 |

超时、失败和取消不写成功缓存。重启后的磁盘恢复须核对引擎和依赖、特征状态与拓扑映射；缺少可验证依据时重算，不冒用旧快照。

## 7. 业务判定、问题定位和异步报告

### 7.1 将有效结果交给规则执行器

本节只规定接入行为；规则字段、OR-of-AND 三态求值和问题计数统一见[本地规则执行](dfm-rule-catalog-database-design.md#55-rule_version)。

每个 G 结果就绪后，更新依赖它的 Check；已有确定结论即可发布，不等待无关任务。保留数值、单位、来源与失败原因，不能把缺失结果填零，也不能从 Check 严重度推断 pass/fail。

例如柱壁最大厚度 2.1 mm、参考壁厚 3.0 mm，规则判比值时两项都是依据，最终形成一个柱壁业务结论。正文不另维护生产阈值或表达式副本。

### 7.2 定位先显示，报告后完成

相关Check可判定就发布结论；失败时立即发布Finding和已有范围，不等待所有组或整个Worker批次。修订使结论过期时发出stale更新并撤下当前统计，历史记录保留。只有部位范围时显示黄色“初步定位”；局部证据满足要求后更新为红色并提示“精确区域已更新”。颜色表示定位状态，不表示严重度。

本例分母为固定参考厚度，可将柱壁各采样厚度除以 3.0，定位比值超出要求的位置。参考主体壁不是违规柱壁，不能因参与公式就全部标红。两个变化场必须有明确位置配对；证据不足时保留初步范围。

每版报告固定result_manifest，核验选中结果与最终确认状态一致，保存采用的快照来源、规则、网格、证据和未完成组。Desktop按该清单查看相同版本；换网格重新映射，不复用旧三角形编号。

Agent整理内容，JobManager后台渲染HTML，定位完善与内容准备可并行。局部方向未齐时可生成阶段报告，明确列出待确认/未覆盖项；不能把已计算项均通过写成全部通过。已发报告绑定不可变清单，后续确认生成新报告版本，失败从原清单重试。

## 8. Hermes 代码修改与验收

| 代码入口 | 改造内容 |
| --- | --- |
| `tools/dfm/analyzers/occt.py`、`tools/dfm/runtime/process.py` | 常驻客户端、协议握手、应答/事件读取、取消与断线恢复 |
| `tools/dfm/feature_recognition/occt_cpp.py`、`tools/dfm/service.py` | 两轮分组确认、结构/方向分支、局部补识别、快照合并、续租和恢复 |
| `tools/dfm/processes/occt_injection.py`、Plan/Operand模型 | 按G能力和确认依赖编排，plan_fragments、完整清单前置、去重与局部失效 |
| `tools/dfm/ontology/` 与规则包 Schema | 对齐权威 G ID 和白名单，保持 Rule 字段名称及判定含义 |
| `tools/dfm/runtime/`、结果/证据/Viewer模型 | provenance与stale处理，JobManager关联批次，result_manifest与报告覆盖状态 |
| `tools/dfm/config.py`、doctor、Desktop 与报告接口 | 会话配置和诊断、进度与恢复提示、统一问题和高亮消费 |

接入顺序：取得已验证的 Worker 契约与配套 EXE → 常驻客户端与快照管理 → Plan 与 G 结果处理 → Desktop/HTML 联调。两侧可并行开发，但只发布通过联调的契约组合。

| Hermes 验收场景 | 通过条件 |
| --- | --- |
| 预览、识别、修订、两批测量 | 调用同一有效模型；通过 Worker 计数确认 STEP 只导入一次 |
| 普通组确认，SIDE_01缺方向，SIDE_02已确认 | 普通组和SIDE_02继续，SIDE_01仅相关方向任务等待 |
| 同base派生快照或确认后修订 | 合并不覆盖，冲突显式处理，仅重算受影响分支，旧结论退出当前统计 |
| 生成阶段报告后继续确认 | 旧报告清单不改写，新报告使用新的有效清单并列明覆盖 |
| 两个 Check 使用相同 G | 只提交一次有效需求，两个 Check 各自判定 |
| 部分 G 失败或缺少方向 | 明确展示原因，AND/OR 判定正确，不假合格 |
| 切换聊天或模型 | 项目与快照不串用；有效句柄可接续，失效句柄显式恢复 |
| Worker 退出、取消、超时 | 任务状态准确，旧句柄不继续使用，不误杀仍服务其他任务的进程 |
| 报告变慢或失败 | 已有问题和定位仍可见，报告可独立重试 |
| Desktop 与 HTML 查看同一 Run | 使用同一网格与定位版本，问题位置一致 |

测试使用仓库 `scripts/run_tests.sh`，覆盖真实 Worker 连接和结果制品，不只使用模拟返回。会话接入通过不等于几何算法完成工程认证。

截至 2026-10-21，上述会话、快照、两轮确认、增量计划、G 结果、定位和报告链路均已按本节验收场景落地。Worker 内部几何数值与识别质量仍按各算法自己的工程认证测试管理，不通过兼容层或会话代码掩盖算法失败。
