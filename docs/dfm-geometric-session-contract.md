# Hermes：Geometric 契约接入与模型会话管理

更新日期：2026-10-14。状态：目标设计，运行代码与 Schema 尚待实施。

总体流程见[DFM 分析主流程](dfm-main-workflow-explained.md)。本方案规定 Hermes 如何调用常驻 Worker、管理项目与快照、组织技术指标计算，以及消费结果和生成报告。

## 1. Hermes 的职责与改造范围

Hermes 负责决定“检查什么、需要算什么、结果是否符合要求”；OCCT Worker 负责识别结构和计算客观几何量。

| Hermes 负责 | 交给 Worker |
| --- | --- |
| 保存上传模型，关联聊天和 DFM 项目 | 加载 STEP，持有实际几何对象 |
| 查询 Feature → Check → Geometric 和规则 | 识别特征，保存内部部位和几何关联 |
| 确认材料、方向等必要输入，固定分析计划 | 按特征和 G ID 选择部位、执行算法 |
| 去重计算需求，管理进度、取消和恢复 | 复用模型与计算资源，逐 G 返回结果 |
| 执行规则，发布问题、定位和 HTML 报告 | 提供数值、测量点、面引用及采样数据 |

当前 `occt.py`、`feature_recognition/occt_cpp.py` 分别调用 EXE；Discovery 虽会保存 JSON，但下一次测量仍需新进程加载模型。本次将预览、识别和测量改为共用常驻连接与模型快照。

开发阶段直接切换新契约，不增加旧协议适配器、字段别名或静默 fallback。不修改 Mold 数据库，不自动删除或转换已有项目和运行产物。

## 2. 共同契约与 Hermes 的使用方式

### 2.1 文档和接口如何维护

两仓库的文档按职责分别编写，不要求全文相同：

| 内容 | 维护位置 | Hermes 的要求 |
| --- | --- | --- |
| G ID 的工程含义、适用部位与取值方法 | 本体规则库 | 使用发布的权威 G ID，不另建 Worker 专用 ID |
| 方法、字段、状态和消息结构 | Worker 仓库 `docs/dfm-geometric-session-contract.md` 第 2 节 | 按同一约定构造请求、校验结果 |
| 可执行契约和联调样例 | 计划由 Worker 仓库 `schemas/` 及契约测试样例统一维护并随版本发布 | 固定所接入的契约制品版本，运行消费端契约测试 |
| 客户端、计划、规则、Viewer 和报告接入 | 当前 Hermes 文档 | 由 Hermes 实现和验证 |

目标主契约为 `dfm.geometry/v2`。正式 Schema 和样例仍待实现；双方确认公共接口变更后，更新同一套定义和样例，不在 Hermes 手工维护另一套近似协议。

契约版本用于校验通信格式；引擎构建及依赖指纹用于追溯与缓存。不能只检查 EXE 版本号，也不能以两篇说明文档的内容哈希相同作为兼容证明。

### 2.2 Hermes 保留哪些标识

| 标识 | 在 Hermes 中的用途 |
| --- | --- |
| `project_id` / `run_id` | 项目与一次分析的记录，不作为 Worker 的计算对象编号 |
| `check_id` | 业务指标，如螺钉柱“柱壁”；留在规则与问题处理层 |
| `snapshot_id` | Worker 返回的模型及特征状态编号，用于后续识别、修订和计算 |
| `feature_id` / `feature_kind` | 快照内的具体结构及其类别，例如 `BOSS_0007` / `screw_boss` |
| `geometric_id` | 两端统一的技术指标 G ID |
| `context` | 该技术指标实际需要的已确认输入及算法参数 |
| `job_id` | Worker 一批几何计算的进度、查询和取消编号 |
| `evidence` | 数值的几何依据，包括面、测量点、局部采样数据和对应快照身份 |

普通计算不再使用 `worker_geometric_id`、`operation_id`、`calculator_id`、`metric_id/metric_ids`、`quantity_id` 路由，也不要求构造 Region 或 `face_indices`。传输层请求 ID 仍用于匹配应答，不再为每个 G 增加 `task_id`。

本体通用表中的 `concept_id` 仍保留，值为该技术指标的 G ID；Worker 的技术指标接口称为 `geometric_id`。规则包的 `properties_json`、`qualifiers_json`、`conditions_json`、`acceptance_criteria_json` 沿用数据库设计中的名称。

### 2.3 Hermes 何时调用各接口

| 方法 | 调用时机 | Hermes 保存或处理什么 |
| --- | --- | --- |
| `capabilities.get` | 连接建立后 | 契约版本、特征与 G 能力、输入要求、单位和证据能力 |
| `snapshots.open` | 没有有效模型快照时 | 新 `snapshot_id`、导入诊断、预览与拓扑映射引用 |
| `features.discover` | 需要识别特征时 | 返回的识别快照、特征清单、识别诊断 |
| `features.revise` | 用户修正特征部位时 | 新快照及修订记录，不覆盖旧 Run 使用的版本 |
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

## 4. 项目与快照：保存记录，使用有效的内存句柄

### 4.1 输入、识别和修订

Hermes 将原始 STEP 保存到项目，记录内容指纹、导入参数和单位。后续调用使用这份输入，不依赖临时聊天附件路径。

```text
PROJECT_A
  ├─ 原始输入：壳体A.stp
  ├─ 当前快照：snapshot_A2
  ├─ 识别与审核记录
  └─ RUN_A1：固定使用 snapshot_A2、指定规则版本和已确认事实
```

打开模型得到 A0；Discovery 返回 A1；修改部位后得到 A2。用户只确认、不修改几何范围时，沿用 A1，由 Hermes 保存审核记录。

普通计算只提交特征与 G ID。用户人工修订则可以提交带拓扑快照身份的面引用，交给 Worker 校验并生成新快照；Hermes 不原地修改原生 Discovery 文件。

### 4.2 聊天结束、快照失效和恢复

| 场景 | Hermes 的处理 |
| --- | --- |
| 用户正在审核、补充事实或等待计算 | 续租仍在使用的快照 |
| 新聊天继续同一项目 | 找到已保存的快照引用，校验有效后复用 |
| 聊天结束，其他聊天或任务仍在使用 | 仅释放当前聊天引用，保留仍需要的资源 |
| 没有使用者 | 关闭快照引用，允许 Worker 按空闲策略回收 |
| 收到 `snapshot_expired` 或 Worker 重启 | 重新打开模型，恢复并校验特征修订，取得新快照，再为后续计算重建绑定 |

恢复时不得把旧面号直接套在新模型上；无法确认对应关系的修订需要重新审核。旧 Run 的记录不原地改写，恢复过程保留来源关系。

磁盘中的 Feature JSON 是识别结果副本，不表示 Worker 内存仍存在。原始输入、Discovery、修订、网格和测量结果按不可变版本保存；项目“当前快照”只是可更新引用，不能覆盖历史 Run 的依据。

## 5. 规则查询与计划：从业务要求生成技术计算需求

Hermes 按以下顺序建立计划：

```text
已确认的螺钉柱 BOSS_0007
  → 查询适用 Check“柱壁”
  → 取得 Check 使用的 G 与公式变量绑定
  → 按 conditions_json 确认材料等事实，选择规则版本
  → 取得 acceptance_criteria_json 的判定要求
  → 校验 G 能力及所需输入
  → 固定 Plan，合并重复的技术指标需求
```

柱壁 Check 的操作数绑定为：

| 公式变量 | 技术指标 |
| --- | --- |
| `boss_wall_thickness` | `G_SCREW_BOSS_WALL_THK_MAX` |
| `adjacent_main_wall_thickness` | `G_SCREW_BOSS_WALL_REF_THK` |

Operand 只是公式变量与 G 的绑定，不新增一套技术指标身份或 CheckOperand 表。Worker 不接收 Check、规则表达式或合格阈值。

一个 Check 可以依赖多个 G，一个 G 也可供多个 Check 使用。同一快照、同一特征、同一 G、相同有效计算条件只保留一次需求。不同参考厚度 G 不能仅凭数值相同合并；参考部位、适用性和取值方法必须一致。

G 的聚合口径固定，例如最大厚度不能在另一个 Check 中改取最小值。若 `USES_OPERAND.qualifiers_json.aggregation` 保留，只允许为空或与 G 定义一致。

Plan 固定输入与特征版本、规则包及 Rule Version、事实来源、计算参数、引擎身份和结果引用。规则包更新进入下一次分析，不改变正在运行的判定条件。

## 6. 计算提交、缓存和结果消费

### 6.1 发给 Worker 的请求

```json
{
  "snapshot_id": "snapshot_A2",
  "items": [
    {
      "feature_id": "BOSS_0007",
      "geometric_ids": [
        "G_SCREW_BOSS_WALL_THK_MAX",
        "G_SCREW_BOSS_WALL_REF_THK"
      ],
      "context": {}
    }
  ]
}
```

这是 `geometrics.compute` 的业务参数。`context` 只保留各 G 实际需要的已确认输入和声明过的算法参数；需要不同 context 的 G 分开组织。主拔模方向不能从默认值或柱轴擅自推断。

Hermes 可以先查已保存的客观结果，只提交缺失项。磁盘缓存匹配要核对输入、特征状态、G 定义、引擎/算法与有效计算参数；不能仅凭文件名或 `feature_id` 命中。恢复后的结果引用需重新验证几何身份，不直接冒用旧快照编号。

改变业务阈值或报告语言通常不必重新测量；改变相关方向、精度或特征范围则需重新验证或计算。失败和取消结果不能充当成功缓存。

### 6.2 收到结果后做什么

| 校验项 | 处理要求 |
| --- | --- |
| 对象身份 | 按快照、特征、G 和有效 context 匹配，不依赖数组顺序 |
| 数值 | 校验状态、类型、单位和有效范围，不把缺失值填成 0 |
| 几何依据 | 检查拓扑、坐标系、单位及显示网格身份 |
| 制品文件 | 校验允许访问的路径、大小、内容哈希和清单，不读取越界路径 |
| 批次状态 | 区分“批次结束”和“所有 G 成功”，保留逐项结果及原因 |

`ok` 表示获得有效值；`unsupported`、`not_applicable`、`missing_context`、`ambiguous_scope`、`failed` 需要保留原因和空值。整批请求的快照失效属于会话错误，不应伪装成某个厚度数值。

算例返回 G1 = 2.1 mm、G2 = 3.0 mm。Hermes 将其绑定回两个公式变量，再按规则计算，不能把 Worker 计算成功直接当作业务合格。

## 7. 业务判定、问题定位和异步报告

### 7.1 按规则条件组判断

`conditions_json` 用于选择适用规则，`acceptance_criteria_json` 用于判断是否合格。没有绝对厚度条件就不自行补充；孔底相对参考壁厚的偏差也应由规则公式表达，Worker 只返回厚度。

算例条件为厚度比 ≤ 0.60：2.1 / 3.0 = 0.70，因此形成一个“柱壁”问题，而不是两个技术指标问题。

| 条件组合 | 判定方式 |
| --- | --- |
| 同一组内的 AND 条件 | 全部通过才通过；任一明确失败则该组失败；无失败但有缺失则无法判断 |
| 多组之间的 OR 条件 | 任一组通过则通过；所有组明确失败则失败；否则无法判断 |
| 缺 G、单位错误、除数为零等 | 对应判定项无法评价，保留原因，再按上述组合规则确定整体结论 |

因此，缺少某个 G 不等于整个 Check 一律未评估。已有完整通过组或已确定失败的组合，仍可得到确定结论。问题按业务类型统计，暂不按高中低风险分级。

### 7.2 定位先显示，报告后完成

规则失败后立即发布 Finding 和已有范围。只有部位范围时显示黄色“初步定位”；局部证据满足要求后更新为红色并提示“精确区域已更新”。颜色表示定位状态，不表示严重度。

本例分母为固定参考厚度，可将柱壁各采样厚度除以 3.0，定位比值超出要求的位置。参考主体壁不是违规柱壁，不能因参与公式就全部标红。两个变化场必须有明确位置配对；证据不足时保留初步范围。

Desktop 与 HTML 使用同一 Run 的网格和已确定的定位版本。换网格重新映射，不能复用旧三角形编号。

Agent 整理报告内容，JobManager 后台渲染 HTML；内容准备可与定位完善并行，最终渲染使用已定稿证据。报告失败可从已有结果重试，不撤销测量和规则结论，也不重新导入 STEP。

## 8. Hermes 代码修改与验收

| 代码入口 | 改造内容 |
| --- | --- |
| `tools/dfm/analyzers/occt.py`、`tools/dfm/runtime/process.py` | 常驻客户端、协议握手、应答/事件读取、取消与断线恢复 |
| `tools/dfm/feature_recognition/occt_cpp.py`、`tools/dfm/service.py` | 预览、识别、审核和正式计算共用快照；管理续租和引用 |
| `tools/dfm/processes/occt_injection.py`、Plan/Operand 模型 | 按 G 校验能力、去重计算，移除公开 Operation/Metric/Quantity/Region 路由 |
| `tools/dfm/ontology/` 与规则包 Schema | 对齐权威 G ID 和白名单，保持 Rule 字段名称及判定含义 |
| `tools/dfm/runtime/`、结果/证据/Viewer 模型 | 逐 G 解析，JobManager 关联 Worker job，固定证据版本 |
| `tools/dfm/config.py`、doctor、Desktop 与报告接口 | 会话配置和诊断、进度与恢复提示、统一问题和高亮消费 |

接入顺序：取得已验证的 Worker 契约与配套 EXE → 常驻客户端与快照管理 → Plan 与 G 结果处理 → Desktop/HTML 联调。两侧可并行开发，但只发布通过联调的契约组合。

| Hermes 验收场景 | 通过条件 |
| --- | --- |
| 预览、识别、修订、两批测量 | 调用同一有效模型；通过 Worker 计数确认 STEP 只导入一次 |
| 两个 Check 使用相同 G | 只提交一次有效需求，两个 Check 各自判定 |
| 部分 G 失败或缺少方向 | 明确展示原因，AND/OR 判定正确，不假合格 |
| 切换聊天或模型 | 项目与快照不串用；有效句柄可接续，失效句柄显式恢复 |
| Worker 退出、取消、超时 | 任务状态准确，旧句柄不继续使用，不误杀仍服务其他任务的进程 |
| 报告变慢或失败 | 已有问题和定位仍可见，报告可独立重试 |
| Desktop 与 HTML 查看同一 Run | 使用同一网格与定位版本，问题位置一致 |

测试使用仓库 `scripts/run_tests.sh`，覆盖真实 Worker 连接和结果制品，不只使用模拟返回。会话接入通过不等于几何算法完成工程认证。
