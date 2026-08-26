---
title: "DFM Hermes Agent 开发路径"
status: active
updated: 2026-08-25
type: product-development-plan
---

# DFM Hermes Agent 开发路径

本文只维护产品目标、阶段范围和验收门槛。系统架构、运行流程、OCCT C++ 外部项目边界和
数据契约见 [DFM 架构、工作流与 OCCT C++ 契约](2026-08-18-dfm-architecture-workflow-and-occt-contract.md)。

## 1. 产品目标

用户提交三维模型和可选二维图纸后，Hermes 应完成：

```text
项目建档 → 信息提取 → 特征发现 → 事实确认 → 分析计划
→ 本体/能力编译 → 客观几何计算 → 确定性规则评价
→ AI工程解释 → 证据截图 → Finding / Report
```

每个结论必须可追溯到输入、事实、特征区域、规则、计算结果和证据。LLM 负责理解、
澄清和解释，不生成几何值、工程阈值或 pass/fail。

## 2. 当前基线

当前正式 Scope：

| 项目 | 当前状态 |
| --- | --- |
| 工艺 | 注塑 `injection` |
| 指标 | 壁厚、拔模角 |
| 三维输入 | PythonOCC 参考实现继续支持 STEP；外部 `dfm-geometry` OCCT C++ 程序已以 experimental Adapter 接入 STEP |
| 二维输入 | 契约和 Provider 占位，尚无生产识别 |
| 特征识别 | 普通全模型区域可运行；主壁、螺钉柱、凸台、筋、孔、倒扣和外观面候选由 OCCT C++ Provider 显式占位 |
| 本体/规则 | Snapshot Schema 2、`injection.default@1.1.0`、本地 SQLite、Check Context 和通用 RuleBinding 编译已实现；中心管理后台与同步尚未交付 |
| 证据 | Hermes 根据 ScalarField 和同源 RenderScene 生成三视角截图 |
| Desktop | 已接入独立 Three.js 3D 查看器，消费 STEP 预览和 Run 的 `dfm_viewer` Manifest |

已完成的基础能力包括项目 Manifest、两阶段发现骨架、区域化 AnalysisPlan、PythonOCC
壁厚/拔模角客观场、统一 Evaluation/Finding/Report，以及 Objective Schema 4 和几何证据
Schema 2；注塑阈值已从静态 Scope 迁移到已发布本体/规则快照，Agent 可以按 Check 输出有限语义
上下文并将本体关系编译为现有规则契约。PythonOCC 只作为参考、契约回归和算法验证实现。独立 OCCT C++ 几何引擎已经完成
本地可执行程序 Adapter、版本化请求/事件/结果、Artifact 校验和真实 E2E 接入，但仍为 experimental；真实工艺特征发现闭环和
生产级 Calculator 认证尚未交付，因此不能声明生产可用。NX/Parasolid 路线延期，
不属于当前里程碑的交付前置条件。

## 3. 开发阶段

### M2.5-A：Agent 本体/规则运行时（基础已完成）

- 冻结 Process、Feature Type、Region Type、Metric、Check、Factor 六类稳定 Concept；
- 使用 Relation 表达 `HAS_CHECK/HAS_REGION/APPLIES_TO_FEATURE/APPLIES_TO_REGION/USES_OPERAND/REQUIRES_FACTOR`；
- 冻结 `OntologyRuleSnapshot` Schema 2，发布物包含本体子图、Factor Option 和有效 Rule Version；
- Agent 将发布包原子安装为 Profile-aware 本地 SQLite，只读执行；
- 通过本体关系编译 `EffectiveRule/RuleBinding`，继续复用通用多 Measurement Evaluation Engine；
- `dfm_analysis context` 按 Check 向 Agent/AI提供有限概念、关系、选项和规则；
- Discovery 的正式 Feature/Region/Metric Target 优先来自已发布本体，不再来自阈值 Scope。

完成证据：修改发布包阈值后 Agent 不改代码即可生成新 EffectiveRule；新增 Feature/Region/Check
本体记录且 OCCT Capability 已声明 Metric 时，通用编译器不增加专用业务分支即可生成 RuleBinding。
Schema 2 已删除 `USES_OPERAND` 中重复的 Worker/Feature/Region Selector，改由
`APPLIES_TO_REGION + HAS_REGION + APPLIES_TO_FEATURE` 唯一解析，并覆盖多 Operand 不同区域测试。

### M2.5-B：Django 本体与规则管理控制面（待实施）

- 建立 Concept、Relation、Factor Option、Rule Version、Rule Set、Rule Set Item、Citation、
  Rule Generation 和 Publication 九张核心表；
- 管理 Web 支持概念/关系字典、完整决策行规则、系统默认与企业覆盖、审核和停用；
- AI 只能根据 Check Context 和知识 Citation 起草 Draft Rule，不自由发明 ID、Operand 或运算符；
- 发布器展开企业继承，并执行 Ontology × OCCT Capability、单位、表达式、规则冲突和引用校验；
- 输出签名 Snapshot，Agent 支持同步、固定版本、回滚和撤销列表。

完成标准：Web、Desktop 和 Agent 读取同一稳定字典；一条审核后的规则可以不发布 Agent 新版本而在
下一次 AnalysisPlan 生效，已有 Run 仍可按旧 Snapshot 完整复现。

### M2.6-A：冻结外部 OCCT C++ 项目边界与契约（Hermes 侧基线已完成，跨仓待实施）

目标是先冻结 Hermes 与独立几何项目的边界，避免 C++ 工程复制规则、项目状态或报告逻辑。

- 独立项目使用 C++17/20 与 OCCT，实现 STEP 几何发现和客观计算；
- 冻结 Geometry Discovery Schema 1、Objective Schema 4、Geometry/Evidence Schema 2；
- Discovery 输出 Observation 候选、Feature、Region、Topology/RenderMesh Snapshot 和同源 Artifact；
- Objective 输出 Measurement、ScalarField、RenderScene 和 TopologyMap；
- 冻结 Capability、错误、进度、取消、Artifact 哈希和版本认证要求；
- Capability 中的 Feature/Region/Metric/Quantity 必须与待发布本体快照做交叉校验；
- Hermes 保留事实、澄清、AnalysisPlan、规则、Evaluation、证据、Finding 和报告；
- PythonOCC 继续作为参考实现和契约回归，不作为生产验收替代品。

完成标准：外部项目不依赖 Hermes 内部 Python 类型即可使用正式 Schema 和共享 Fixture 完成
Discovery 与 Objective 请求/结果的双向契约测试。

### M2.6-B：OCCT C++ 生产闭环（待实施）

用 STEP、壁厚和拔模角完成第一条生产链路，同时交付第一批真实 Feature/Region。

- STEP Loader、单位/容差规范化、拓扑检查和受控 Shape Healing；
- 产生不可变 GeometrySnapshot、TopologySnapshot、RenderMeshSnapshot 和 TopologyMap；
- 第一阶段发现几何独立特征和出模方向候选，候选不得自动变成 confirmed Fact；
- 用户确认出模方向后执行方向相关识别，包括倒扣与外观面候选；
- 实现工程师批准的主壁、螺钉柱、凸台、筋和孔等首批特征；
- Feature/Region 与 TopologySnapshot 绑定，ordinary 是已计算特征区域的补集；
- 对批准 Region 执行壁厚和拔模角 Calculator，输出完整客观场与控制极值；
- Hermes 完成规则评价、FailedPatch、证据、Finding 和报告；
- 通过真实产品、对抗模型、并发稳定性和 PythonOCC 参考回归验收。

完成标准：任一 Finding 能从报告反向追溯到图片、高亮三角形、场值、拓扑实体、Feature/
Region、Operation、规则、输入哈希、Snapshot 和 C++ 实现版本。

### M2.6-C：特征规则与指标逐项扩展（待实施）

按工程价值逐项加入，不一次性实现候选清单。推荐顺序：

1. 主壁、螺钉柱、凸台、筋的区域化壁厚和拔模角；
2. 根部 R 角、孔深及与主壁相关的几何关系；
3. 倒扣、滑块/斜顶相关区域；
4. 经工程评审批准的其它几何指标。

每个增量都必须同时交付 Recognizer/Region、Calculator、规则、证据、Golden Model 和工程
验收，不能只增加字段或报告文案。压铸等其它工艺必须建立独立 Scope 和认证范围，不复制
注塑阈值冒充支持。

### M2.7：黄金产品完整闭环（待实施）

- 冻结真实或脱敏黄金产品、确认事实和批准规则；
- 覆盖该产品全部批准指标；
- 生成不可变 Run Bundle；
- 由模具工程师人工核对 Measurement、区域、Finding、证据和报告并签字。

Ground Truth 只用于研发验收，不进入生产分析，也不回写运行结果。

### M3：二维图纸信息提取（待实施）

- PDF/图片解析、OCR、版面和表格识别；
- 输出带页码、bbox、原文、单位和置信度的 Observation；
- 高置信度且无冲突的信息可转为 Fact，歧义进入 Clarification；
- 无可靠比例或明确标注时，不从像素推断精确几何尺寸。

### M4：二维工程特征与三维融合（待实施）

- 识别公差、材料、表面要求、基准和局部工程标注；
- 将二维 Observation 与三维 Feature/Region 建立可审核 FusionLink；
- 冲突和低置信度映射由用户确认；
- 图纸信息参与规则选择，但不替代三维客观计算。

### M5：平台化与多工艺扩展（待实施）

- 通用 Capability/Calculator 注册与认证；
- 受影响 Operation 重算和断点复用；
- 租户、项目、Run 和 Artifact 隔离；
- 新工艺通过 ProcessAdapter、独立事实和独立规则 Scope 接入；
- 生产部署、权限、审计、监控和容量验证。

## 4. 全阶段不变量

1. Manifest 是项目事实来源，聊天记录不是数据库。
2. 先发现后分析：冻结 DiscoverySnapshot 后才编译 AnalysisPlan。
3. Backend 只做特征识别和客观计算；Hermes 持有规则、Evaluation、证据和 Finding。
4. PythonOCC 参考实现与 OCCT C++ 生产实现允许算法和精度不同，但数据契约与后处理流程必须一致。
5. 选择 OCCT C++ Production 后禁止静默降级到 PythonOCC。
6. 未实现、低置信度或未认证能力必须显式阻塞或回退为 ordinary，不生成伪特征。
7. 修改规则只重做评价闭包；修改输入、拓扑、网格或算法版本会使相关客观缓存失效。
8. 新能力保持在 DFM toolset/服务边缘，不修改 Hermes Agent Loop，不增加无关会话工具负担。
9. NX 与 Parasolid 保留为未来可选 Backend；延期不得污染 OCCT C++ 当前契约或阻塞 STEP 生产闭环。
10. 管理库是编辑来源；Agent 本地库是一次发布的只读投影，运行中不得逐行修改或切换版本。
11. 本体只有通过通用编译器、Check Context 或发布校验被消费才允许存在，禁止建设无人使用的概念表。

## 5. 验收方式

| 层级 | 要求 |
| --- | --- |
| Contract | JSON Schema、共享 Fixture、跨 Run/输入/快照错配负例 |
| Component | Recognizer、Calculator、Rule、Evidence 各自行为测试 |
| Integration | 上传、Job、取消、失败恢复、Artifact 哈希和缓存恢复 |
| E2E | Desktop/CLI 创建项目到报告；PythonOCC 参考 STEP；真实 OCCT C++ STEP |
| Engineering | 数值容差、问题区域重叠、截图可读性和模具工程师签字 |

## 6. 当前优先级

后续按两条主线并行推进，几何生产闭环是产品核心路径，规则控制面不能阻塞 OCCT 工程启动：

| 优先级 | 几何生产主线 | 规则与 Agent 主线 |
| --- | --- | --- |
| P0 | 建立独立 `dfm-occt-worker` 仓库；冻结 Discovery 1、Objective 4、Geometry/Evidence 2 和共享 Fixture | 固定 Snapshot Schema 2；继续维护本地编译、Evaluation 和契约测试 |
| P1 | STEP Loader、Snapshot、主壁/螺钉柱等首批 Feature/Region Recognizer、壁厚/拔模 Calculator | 独立 Django 工程实现九张中心表、审核发布器和 Snapshot API |
| P2 | 用螺钉柱壁厚比例完成“不改 Agent 业务代码”的 Golden E2E | Agent 完成签名同步、版本选择、回滚和撤销处理 |
| P3 | 并发、资源隔离、数值与工程认证后逐项扩展指标 | 规则生成 AI、知识 Citation、企业规则管理和审计 |

二维图纸识别与 2D/3D Fusion 在三维 Golden E2E 之后进入实现，但其 Observation、FusionLink 和
Clarification 契约继续保留，避免后续破坏主数据链。

文档只记录已批准方向。字段和状态以 `tools/dfm/schemas/`、`tools/dfm/contracts.py` 和
`tools/dfm/scopes/` 为当前可执行依据；中心管理后台交付后，以签名发布 Snapshot 和对应 Schema 为
最终依据。
