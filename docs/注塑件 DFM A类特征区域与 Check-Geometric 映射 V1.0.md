# 注塑件 DFM A类特征区域与 Check-Geometric 映射 V1.0

日期：2026-09-29。

输入资料：`D:\notes\MoldMind\V1.2.5.xlsx`。

关联文档：[特征识别与几何计算开发任务清单](注塑件%20DFM%20特征识别与几何计算开发任务清单%20V1.0.md)、[检查项覆盖矩阵](注塑件%20DFM%20检查项覆盖矩阵%20V1.0.md)。

本文先用螺钉柱还原Excel的设计思路，再检查现有“功能区指标”对A类44行的覆盖，最后给出可落地的Feature、Check和Geometric映射。区域和对象关系由Geometric ID的定义携带，由OCCT Worker在内部定位；本文中的区域名称用于说明每个Geometric必须理解的目标语义，不建议新增独立的Region Role或Relation ID体系。标为“新增”的`C_*`/`G_*`是建议命名，正式发布前应与现有数据字典统一冻结。

## 1. 对Excel设计思路的理解

Excel目前表达的是一条正确的规则执行链：

```text
Feature实例
  → 目标Region及其关系
  → Geometric客观量
  → Check业务指标
  → Factor选择/修正规则
  → Rule比较表达式
  → Finding及几何证据
```

各层职责应保持如下边界：

| 层 | 回答的问题 | 示例 |
| --- | --- | --- |
| Feature | 这是什么结构实例 | 螺钉柱、筋、盲孔、卡扣 |
| Geometric的区域语义 | 这个实例的哪个部位参与量测 | 螺钉柱外壁、孔壁、孔底、外根部；编码在Geometric定义中 |
| Geometric的配对语义 | 与谁比较、如何连接或如何选参考 | 螺钉柱邻近主壁、孔底对应外壁、BOSS支撑筋集合 |
| Geometric结果 | 客观几何值及证据是什么 | 局部厚度场、最小R、带符号拔模角、净距，以及相关Face/Edge位置 |
| Check | 要检查的业务指标是什么 | 螺钉柱壁厚、孔底厚度、BOSS加强筋 |
| Factor | 哪条规则适用、阈值如何修正 | 材料、外观区、工艺、方向、钢材类型 |
| Rule | 如何比较Geometric与阈值 | `min_ratio >= lower && max_ratio <= upper` |

这个方向适合继续落地。根据当前方案，Feature和Geometric之间不需要再暴露独立的Region Role和Relation模型：`CORE、BOTTOM、OUT、IN、ROOT`等词汇共同构成Geometric ID的固定语义，OCCT Worker按该定义寻找目标区域、参考区域或对象配对，并在Measurement中返回相关Face/Edge/Volume位置。需要补强的不是一套新ID，而是**每个Geometric ID的可执行契约**，保证名称、区域选择、聚合口径和证据返回在各实现间保持一致。

## 2. 用螺钉柱还原完整模型

### 2.1 螺钉柱Geometric内部需要理解的区域和配对

| 类型 | 内部目标语义 | 含义 | 结果中应返回的证据 |
| --- | --- | --- | --- |
| Feature | 螺钉柱实例 | 已确认螺钉用途的中空BOSS | `feature_id`及组成拓扑引用 |
| 自身区域 | 柱体外壁 | 柱体外侧成型壁 | 参与量测的Faces、极值位置 |
| 自身区域 | 螺钉孔壁 | 螺钉孔/芯针形成的内壁 | 参与量测的Faces、有效段 |
| 自身区域 | 柱顶环面 | 柱顶环形端面 | Faces及边界环 |
| 自身区域 | 盲孔底 | 通孔时不存在 | 底面Faces及底部类型 |
| 自身区域 | 柱外根部过渡 | 柱外壁与承载壁之间的根部blend | Faces/Edges、最小R位置 |
| 自身区域 | 孔底内圆角 | 孔壁与孔底之间的blend | Faces/Edges、最小R位置 |
| 自身区域 | 柱体实体域 | 用于厚肉、体积和截面分析 | Solid/Volume及截面证据 |
| 派生参考 | 轴线/中心轨迹 | 由孔壁或柱壁拟合 | Axis/Curve、拟合误差 |
| 外部参考 | 邻近主体壁 | 与该柱连接且用于取参考壁厚 | 被选主壁Faces及选择依据 |
| 外部参考 | 孔底对应外壁 | 与孔底构成底厚的产品外表面 | 配对Faces、见证点/线 |
| 特征集合 | 支撑筋集合 | 与柱体及主壁有效连接的筋 | 支撑筋`feature_id`列表和连接证据；没有时返回空集合 |
| 外部配对 | 最近有效外壁 | 用于BOSS外侧净距，排除根部共边 | 被选外壁、见证点和排除原因 |

这些区域和配对不需要各自拥有对外ID。它们是相应Geometric ID定义的一部分。例如`G_SCREW_BOSS_BOTTOM_THK_MIN`天然要求Worker找到“该螺钉柱的盲孔底”和“与其对应的产品外壁”；结果除数值外，还应返回这两组面的拓扑引用和最小厚度见证点。这样规则失败后可以直接高亮证据。

### 2.2 螺钉柱Check与Geometric的正确对应

| Check | 目标区域/关系 | 依赖Geometric | 对现表的建议 |
| --- | --- | --- | --- |
| `C_SCREW_BOSS_WALL_THK` | 外壁＋孔壁＋局部主壁参考 | `G_SCREW_BOSS_WALL_THK_FIELD/MIN/MAX`、`G_SCREW_BOSS_WALL_REF_THK` | 现表只有`MAX/REF`。同时存在下限和上限时，应以`MIN`发现过薄、以`MAX`发现过厚；单用最大值会漏掉局部过薄 |
| `C_SCREW_BOSS_HOLE_DEPTH` | 孔壁＋孔底＋轴线 | `G_SCREW_BOSS_CORE_DEPTH`、`G_SCREW_BOSS_HOLE_DIA_EFFECTIVE`、二者比值 | “取孔底处孔径”对锥底、阶梯孔不充分；需定义有效成型段和控制直径。规则页第二分支表达式为`≤4`，改进建议却写“超过3:1”，需统一 |
| `C_SCREW_BOSS_BOTTOM_THK` | 孔底＋对应外壁＋局部主壁参考 | `G_SCREW_BOSS_BOTTOM_THK_MIN`、`G_SCREW_BOSS_WALL_REF_THK` | 将现有`BOTTOM_THK`明确为配对区域内最小底厚，并返回见证点/线 |
| `C_SCREW_BOSS_DRAFT` | 外壁、孔壁＋用户主出模方向 | `G_SCREW_BOSS_DRAFT_OUT_FIELD/MIN`、`G_SCREW_BOSS_DRAFT_IN_FIELD/MIN` | A类统一使用用户提供的`F_TOOL_MAIN_DRAW_DIR`并分别处理内外壁符号；孔轴/柱轴只能用于一致性检查，不能替代主方向。若需要侧向芯针，先返回主方向下不适用/倒扣证据，取得局部方向后转相应扩展检查 |
| `C_SCREW_BOSS_OUTER_ROOT_FILLET` | 外根过渡＋局部主壁参考 | `G_SCREW_BOSS_OUTER_ROOT_FILLET_R_MIN`、`G_SCREW_BOSS_WALL_REF_THK` | 现有名称`G_SCREW_BOSS_FILLET_TOP_R`与规则页“外根部”不一致，建议迁移为外根部ID并保留旧ID别名 |
| `C_SCREW_BOSS_HOLE_BOTTOM_FILLET` | 孔底内圆角＋局部主壁参考 | `G_SCREW_BOSS_HOLE_BOTTOM_FILLET_R_MIN`、`G_SCREW_BOSS_WALL_REF_THK` | 不应与外根部圆角共用一个Check，否则不同区域、风险和改进建议无法独立出Finding |
| `C_BOSS_ROOT_CONNECTION` | 柱根＋主壁＋支撑筋关系 | `G_BOSS_ROOT_THK_MAX`、`G_BOSS_ROOT_CONTACT_AREA`、`G_BOSS_ROOT_FILLET_R_MIN` | A类01.08，现表缺少 |
| `C_SCREW_BOSS_RIB_SUPPORT` | 螺钉柱＋支撑筋集合＋主壁 | `G_BOSS_SUPPORT_RIB_COUNT`、筋尺度、连接和根R | A类11.02；即使没有筋也要从螺钉柱触发，返回数量0及“期望关系缺失”证据 |
| `C_BOSS_TO_OUTER_WALL_CLEARANCE` | 柱外壁/根与最近外壁的指定配对 | `G_BOSS_TO_OUTER_WALL_MIN_DIST` | A类11.04；不要用根部共边产生的零距离，也不要直接等同薄钢结论 |
| `C_LOCAL_THICK_MASS` | 柱根实体域＋背面区域 | `G_LOCAL_THICK_MASS_EQ_THK_MAX`、`G_LOCAL_THICK_MASS_VOLUME` | A类01.04，现表缺少 |

现有`C_SCREW_BOSS_THIN_STEEL`需要钢材类型、成型侧、钢料高度和支撑条件。产品几何可以先输出BOSS到外壁净距作为预筛，但“薄钢是否合格”应放在B类模具对象补齐后确认，不应占用A类11.04的几何净距Check。

## 3. Geometric内部的特征区域应怎样划分

### 3.1 四类目标必须分开

| 目标类型 | 谁负责定位 | 示例 |
| --- | --- | --- |
| Feature实例 | 特征识别阶段返回 | 筋、BOSS、盲孔、卡扣及其`feature_id` |
| Feature自身部位 | 对应Geometric算法在Feature组成拓扑内定位 | 筋侧壁、BOSS外壁、盲孔底、卡扣根 |
| 外部参考或对象配对 | 对应Geometric算法按其固定选择语义定位 | 筋邻近主壁、孔底对应外壁、BOSS附近外壁 |
| 全局/未分类作用域 | 特征识别或模型基础分析提供范围，Geometric执行量测 | 整件、全部有效侧壁、未分类薄壁区 |
| 方向或量测派生区域 | Geometric产生，规则可进一步筛选 | 倒扣区、零拔模区、反拔模区、厚肉区、超小特征候选 |

不能要求特征识别器在不知道方向、量测结果或规则阈值时直接给出“零拔模区”“超限厚肉区”。识别器先返回稳定的Feature实例和全局作用域；Geometric根据ID语义定位面并返回数值/场及证据；Hermes再按规则筛出违规证据。

### 3.2 推荐请求和结果模型

- `snapshot_id`：绑定模型版本；拓扑变化后旧证据不得静默复用。
- `feature_id`或`scope_id`：指定在哪个Feature实例或全局作用域执行，例如`SCREW_BOSS_0007`。
- `geometric_id`：唯一确定特征类型、内部目标区域、参考对象、计算量和聚合口径。
- `context`：A类方向相关任务必须携带用户提供的主出模方向、来源和坐标系；还可包含成型侧、比较基准或方案。方向改变时，相关结果必须重新计算。
- `value/field`、`quantity`、`unit`和`aggregation`：返回客观计算结果和口径。
- `evidence_refs`：返回参与计算、极值或违规定位所需的Face/Edge/Volume引用、见证点/线和可视化网格映射。
- `selected_operands`：当Geometric内部选择了邻近主壁、对应外壁、另一孔或支撑筋集合时，返回实际选中的对象及选择质量。
- `status/diagnostics`：成功、无适用区域、输入缺失、歧义、不支持和计算失败必须区分。

不要求为每个内部区域建立持久Region ID。相同Face可以成为多个Geometric结果的证据；每个Measurement通过`geometric_id + feature/scope + context + snapshot`建立缓存身份，并携带本次计算实际使用的拓扑引用。

### 3.3 Geometric ID最低契约

| 字段 | 必须定义的内容 |
| --- | --- |
| 目标Feature/Scope | 适用于`SCREW_BOSS`、`RIB`、`HOLE`还是整件；是否支持子型继承 |
| 内部目标区域 | 外壁、孔壁、孔底、根部、实体域等如何识别，允许哪些拓扑变体 |
| 外部参考/配对 | 邻近主壁、对应外壁、配对孔、支撑筋如何选择；歧义时如何处理 |
| 上下文 | A类主方向必须取用户Fact；说明正/负半模符号、坐标系和成型侧。局部退出方向只在补充输入后用于侧向扩展任务 |
| 计算定义 | 厚度、距离、深度、R、角度、路径和截面的精确定义 |
| 聚合口径 | 返回局部场、最小值、最大值、分位值还是多个分支值 |
| 适用与空结果 | 通孔没有孔底、实心BOSS没有内壁、没有支撑筋时分别返回什么 |
| 证据 | 需要返回哪些面、边、体、见证点/线、极值位置和匹配质量 |

### 3.3 划分粒度原则

1. 按几何操作数区分内部区域：螺钉柱外壁和孔壁必须分开，因为方向和拔模阈值不同，但无需为两者发布独立Region ID。
2. 按部位语义定位，不按业务阈值定位：Worker可以识别筋根；“R/t不合格部分”由规则结果确定。
3. 参考对象的选择写进Geometric契约：主壁参考、对应外壁、对称配对和邻近对象不能靠各算法临时采用不同策略。
4. 保留未分类作用域：未识别为筋/BOSS的面仍须进入基础厚度、拔模和圆角扫描。
5. 允许候选和歧义：多个可能主壁、多个可能对称面时，Measurement返回候选、匹配质量或歧义状态，由Hermes询问确认。
6. Feature子型复用Geometric实现：`SCREW_BOSS`是`HOLLOW_BOSS`的业务子型，可复用BOSS外壁、内壁、根部和主壁参考算法，再增加螺钉用途和孔底语义。

### 3.4 主出模方向的输入和执行边界

`F_TOOL_MAIN_DRAW_DIR`是启动阶段由用户提供的必需Fact，不是特征识别输出，也不是OCCT需要搜索的Geometric。方向的实际向量不编码进Geometric ID，而是由Hermes解析Fact后作为本次Geometric请求的Context传入。其执行约定如下：

1. 输入统一转换到当前模型坐标系，校验为非零向量并归一化；保留原始输入、转换矩阵和用户来源。
2. 所有A类04类、05类方向相关Geometric都消费同一个主方向Context；结果和缓存键必须包含方向值及坐标系。
3. Worker不得以包围盒主轴、面积最大投影方向、孔轴、BOSS轴或法向统计结果静默替换用户方向。
4. `+D/-D`用于两侧开模和带符号角度/可见性计算，具体正负半模约定必须在Geometric契约中统一。
5. 孔轴、BOSS轴、筋延伸方向等是几何测量结果，可用于报告与主方向的夹角或发现侧向成型需求，但不是新的脱模方向Fact。
6. 某Feature沿主方向不可成型时，A类返回倒扣、不适用或需要局部方向的证据；只有用户/模具方案补充侧抽、斜顶或芯针方向后，才执行局部方向检查。
7. 缺少主方向时，方向相关Check状态为`MISSING_REQUIRED_INPUT`，不能自动猜测并输出合格/不合格；不依赖方向的厚度、尺寸和圆角任务可以独立执行。

## 4. Excel现有Check对A类44行的覆盖

按“是否已有对应Check框架和Geometric名称”统计，不代表算法已经实现：

| 状态 | A类原检查行数 | 说明 |
| --- | ---: | --- |
| 已有直接对应框架 | 16 | 壁厚范围、筋厚、孔底厚、各类基础拔模/根R、螺钉柱壁厚、孔边/孔间距等 |
| 可复用现有Geometric但需新增/拆分Check | 7 | 壁厚均匀性、普通BOSS壁厚、零/反拔模、外圆角、BOSS外壁距、普通盲孔深径比 |
| 当前未表示 | 21 | 厚肉/交汇、倒扣、卡扣、对称、止口、悬臂、孔筋距、深槽、超小特征等 |
| 合计 | 44 | 其中01.09与12.03是同一业务概念的重复来源行 |

现有Excel还存在以下结构性缺口：

- `C_RIB_SPACING`被用于“筋薄钢”占位，ID语义不一致；应撤销该映射，规则未定义前保持TBD，不要复用错误Check ID。
- `C_SCREW_BOSS_FILLET`同时容纳外根部和孔底圆角，建议拆成两个Check。
- `G_SCREW_BOSS_FILLET_TOP_R`与“外根部圆角”的文字定义不一致。
- 壁厚、拔模和圆角已有数值ID，但Geometric契约尚未完整说明内部区域选择、有效域、统计口径和见证几何。
- 普通BOSS没有壁厚Check；现有柱壁厚只覆盖螺钉柱子型。
- `C_HOLE_CLEARANCE`包含孔到孔、孔到外壁两种操作数，执行时必须通过子指标或Check参数区分，不能只返回一个无语义的最小距离。

## 5. A类44项是否需要调整

建议保留44个原编号作为需求来源和报告追溯，但执行模型做以下规范化：

1. **合并重复来源**：01.09和12.03统一映射到`C_HOLE_BOTTOM_THK`，只执行一次量测和规则判断，报告可保留两个来源编号。
2. **合并同一范围检查**：01.02和01.03继续使用Excel已有的`C_WALL_THK_RANGE`，分别产生`BELOW_MIN`和`ABOVE_MAX`原因；不要建立两套厚度算法。
3. **规范零/反拔模**：05.06和05.07统一为`C_DRAFT_SIGN`规则族，零拔模和反拔模作为不同Finding reason；特征专属拔模Check仍保留。
4. **重命名04.01**：启动阶段必须取得用户主出模方向，业务含义应为“用户给定主出模方向的可脱模性评价”。OCCT不得生成候选方向或自动替换输入；若未来需要推荐方向，应另立外层方案搜索能力，不混入该Check。
5. **保留泛型与子型**：01.07普通BOSS壁厚和11.01螺钉BOSS壁厚不是重复项。对已确认的`SCREW_BOSS`只执行更具体规则，避免同一实例重复报普通BOSS结果。
6. **将09.01/09.02设为条件性A类**：只有存在已确认的比较区域/对称基准时执行；可以自动提出候选基准，但歧义时必须询问。
7. **缺失结构由父Feature触发Geometric**：11.02不能只在已识别到筋时触发。应由BOSS触发“需要支撑筋”Check，再调用支撑筋数量Geometric；`support_rib_count=0`是合法结果，并返回空集合证据。
8. **薄钢分层**：A类11.04只计算产品几何净距；真实薄钢规则在B类取得模具域、成型侧和钢材Factor后确认。

按此规范，44个来源行对应**41个执行层业务Check**：01.09/12.03合一、01.02/01.03合一、05.06/05.07合一。共享Check不影响报告继续显示原检查编号。

## 6. A类需要识别的Feature及Geometric内部区域清单

### 6.0 完整Feature结构

下面是当前A类44项所需的完整结构。`[F]`表示特征识别阶段输出的Feature实例，`[S]`表示全局作用域，`[D]`表示Geometric计算时建立的派生分析域。树表达类型继承和主要包含关系，不表示每个部位都要发布独立Region ID。

```text
PART [S]：整个塑件/当前分析实体
├── PART_VALID_SURFACE [S]：全部有效产品表面
├── PART_UNCLASSIFIED_SURFACE [S]：未归入具体语义Feature的表面
├── WALL [F/S]：主体壁
│   ├── 外侧壁、内侧壁、有效厚度域、局部参考壁
│   └── WALL_TRANSITION [D]：厚侧、薄侧、过渡路径、过渡圆角
├── RIB [F]：加强筋
│   ├── 两侧壁、根、顶、端、实体域、邻近主壁
│   └── RIB_INTERSECTION [D]：多筋交汇域、参与筋集合
├── BOSS [F]：普通凸台/柱体
│   ├── 外壁、可选内壁、顶、根、根部圆角、实体域、轴线
│   └── SCREW_BOSS [F]：螺钉用途BOSS子型
│       ├── 继承BOSS全部部位
│       └── 孔壁、孔底、孔底圆角、顶环面、支撑筋集合
├── HOLE [F]：孔/开口父类型
│   ├── HOLE_THROUGH [F]：通孔
│   │   └── 孔壁、两端开口、有效成型段、轴线
│   ├── HOLE_BLIND [F]：盲孔
│   │   └── 孔壁、开口、有效段、轴线、孔底、孔底圆角、对应外壁
│   └── OPENING_NONROUND [F]：非圆开口
│       └── 开口边界、侧壁、有效通道、可选底面；无定义时不套用圆孔L/D
├── CANTILEVER [F]：通用悬臂结构
│   ├── 梁段、固定根、自由端、中性路径、控制截面
│   └── SNAP [F]：带钩部/扣合用途的卡扣子型
│       └── 继承悬臂部位，并增加钩部、卡扣侧壁、约定加载点
├── LIP_OR_LEDGE [F]：止口/台阶薄壁
│   └── 薄壁、台阶面、根部、相邻母壁、对向壁
└── SLOT_OR_DEEP_CAVITY [F]：深窄槽/腔
    └── 槽口、两侧壁、底面、有效深度路径、最窄截面

跨Feature分析视图
├── GENERIC_FILLET [F/D]：内凹/外凸圆角、母面、过渡边
├── THICK_MASS_CANDIDATE [D]：厚肉实体域、最大截面、背面对应区
├── DRAFT_SCOPE [D]：用户主出模方向下的全部有效侧壁
│   ├── UNDERCUT_EVIDENCE [D]：内/外倒扣连通域及深度见证
│   └── DRAFT_SIGN_EVIDENCE [D]：零拔模/反拔模证据域
├── SYMMETRY_COMPARISON_SET [D]：两组壁厚域或筋网络及对应关系
└── MICRO_FEATURE_CANDIDATE [D]：小面、小边、小孔/槽/凸起候选
```

`SCREW_BOSS`同时具有BOSS子型和“包含孔结构”的组合语义；一个螺钉柱实例不需要再复制成一个互不相关的普通BOSS和普通孔。Worker可以复用BOSS/HOLE底层算法，但Hermes应按最具体的Feature类型选择Check，避免重复Finding。

### 6.0.1 Feature、内部区域、Check与Geometric总表

| 层级 | Feature/Scope | Geometric内部必须理解的完整区域语义 | 对应A类Check来源 | 主要Geometric |
| --- | --- | --- | --- | --- |
| 全局作用域 | `PART` | 整件实体、有效表面、连通体、模型坐标系 | 04.01—04.04、05.06/05.07、12.08 | `G_PART_VISIBLE_AREA_POS/NEG`、`G_PART_UNDERCUT_AREA`、`G_DRAFT_SIGNED_FIELD`、`G_FEATURE_MIN_SCALE` |
| 全局作用域 | `PART_UNCLASSIFIED_SURFACE` | 未归入WALL/RIB/BOSS等Feature但仍需接受基础扫描的表面 | 01.01—01.03、05.06/05.07、06.01/06.02、12.08 | `G_WALL_THK_LOCAL`、`G_DRAFT_SIGNED_FIELD`、`G_FILLET_R_MIN`、`G_FEATURE_MIN_SCALE` |
| 主体结构 | `WALL` | 外侧壁、内侧壁、有效厚度域、对向面、局部参考壁 | 01.01—01.03、05.01/05.02、09.01 | `G_WALL_THK_LOCAL/MIN/MAX/P05/P95`、`G_WALL_OUT/IN_DRAFT_FIELD/MIN`、`G_WALL_PAIR_THK_DIFF_MAX/MEAN` |
| 派生结构 | `WALL_TRANSITION` | 厚侧A、薄侧B、过渡路径、有效过渡长度、直角台阶、过渡blend | 01.10、06.06 | `G_WALL_TRANS_T1/T2/DT/LEN/STEP_COUNT/GRAD_MAX`、`G_WALL_TRANS_FILLET_R_MIN` |
| 通用过渡 | `GENERIC_FILLET` | 内凹blend、外凸blend、相邻母面、过渡边、凹凸属性，以及未倒圆的内/外尖边候选 | 06.01/06.02 | `G_WALL_IN_FILLET_R_MIN`、`G_WALL_OUT_FILLET_R_MIN`、曲率、拟合误差；尖边按R=0或单独状态返回 |
| 工程Feature | `RIB` | 两侧壁、根、顶、端、实体域、邻近主壁参考、方向和高度范围 | 01.05、05.03、06.03、09.02、11.02、12.06 | `G_RIB_ROOT_THK`、`G_RIB_WALL_REF_THK`、`G_RIB_DRAFT_SIDE_A/B_FIELD/MIN`、`G_RIB_ROOT_FILLET_R`、布局量和距离量 |
| 派生结构 | `RIB_INTERSECTION` | 交汇实体域、参与筋集合、交汇根部、邻近主壁 | 01.06 | `G_RIB_INTERSECTION_EQ_THK_MAX`、`G_RIB_INTERSECTION_VOLUME`、`G_RIB_WALL_REF_THK` |
| 工程Feature | `BOSS` | 外壁、可选内壁、顶、根、根部blend、实体域、轴线、局部主壁、最近有效外壁、支撑筋集合 | 01.07/01.08、05.04、06.04、11.02/11.04 | `G_BOSS_WALL_THK_FIELD/MIN/MAX`、`G_BOSS_ROOT_THK_MAX`、`G_BOSS_DRAFT_OUT/IN_FIELD/MIN`、`G_BOSS_ROOT_FILLET_R`、`G_BOSS_SUPPORT_RIB_COUNT`、`G_BOSS_TO_OUTER_WALL_MIN_DIST` |
| Feature子型 | `SCREW_BOSS` | 继承BOSS；增加孔壁、孔底、孔底blend、顶环面和螺钉用途 | 11.01；按结构复用01.09、05.04、06.04、11.02/11.04、12.02/12.03 | `G_SCREW_BOSS_WALL_THK_FIELD/MIN/MAX`、`G_SCREW_BOSS_WALL_REF_THK`、`G_SCREW_BOSS_CORE_DEPTH`、`G_SCREW_BOSS_HOLE_DIA_EFFECTIVE`、`G_SCREW_BOSS_BOTTOM_THK_MIN`、内外拔模和两类圆角量 |
| Feature父型 | `HOLE` | 孔壁、开口、有效成型段、轴线；按子型决定是否有第二开口或孔底 | 12.01—12.06及01.09 | 复用通孔/盲孔Geometric，不直接产生父型Finding |
| Feature子型 | `HOLE_THROUGH` | 孔壁、入口/出口、有效贯通段、轴线、有效直径 | 12.01、12.04—12.06 | `G_HOLE_EFFECTIVE_LEN`、`G_HOLE_DIA_EFFECTIVE_MIN`、`G_HOLE_LD_RATIO`、孔到壁/孔/筋距离 |
| Feature子型 | `HOLE_BLIND` | 孔壁、开口、有效段、轴线、孔底、孔底blend、对应外壁 | 01.09、12.02—12.06 | `G_HOLE_EFFECTIVE_DEPTH`、`G_HOLE_DIA_EFFECTIVE_MIN`、`G_HOLE_BOTTOM_THK`、`G_HOLE_LD_RATIO`、孔到壁/孔/筋距离 |
| Feature子型 | `OPENING_NONROUND` | 非圆开口边界、侧壁、有效通道、可选底面和对应外壁 | 05.02、12.04/12.06、12.08；仅在规则定义后参与12.01/12.02 | 拔模角场、到壁/筋距离、最小开口尺度；未定义等效直径时不输出圆孔`L/D` |
| 工程Feature | `CANTILEVER` | 梁段、固定根、自由端、中性路径、控制截面 | 11.11 | `G_CANTILEVER_LEN`、`G_CANTILEVER_SECTION_THK_MIN/WIDTH`、`G_CANTILEVER_SLENDERNESS` |
| Feature子型 | `SNAP` | 继承CANTILEVER；增加钩部、卡扣侧壁、有效加载点、局部根部过渡 | 05.05、06.05、11.06/11.07 | `G_SNAP_DRAFT_FIELD/MIN`、`G_SNAP_HOOK_UNDERCUT_DEPTH`、`G_SNAP_CANTILEVER_LEN`、`G_SNAP_ROOT_THK/FILLET_R_MIN/TRANS_LEN` |
| 工程Feature | `LIP_OR_LEDGE` | 止口薄壁、台阶面、根部、相邻母壁、对向壁 | 11.08 | `G_LIP_WALL_THK_FIELD/MIN/MAX`、`G_LIP_STEP_HEIGHT` |
| 工程Feature | `SLOT_OR_DEEP_CAVITY` | 槽口、两侧壁、底面、有效深度路径、最窄截面 | 12.07 | `G_SLOT_DEPTH_EFFECTIVE`、`G_SLOT_WIDTH_MIN`、`G_SLOT_HW_RATIO` |
| 派生分析域 | `THICK_MASS_CANDIDATE` | BOSS/筋根/交汇等厚肉实体域、最大截面、背面对应区 | 01.04/01.06 | `G_LOCAL_THICK_MASS_EQ_THK_MAX`、`G_LOCAL_THICK_MASS_VOLUME/SECTION_MAX` |
| 派生分析域 | `DRAFT_SCOPE` | 用户主出模方向下全部有效侧壁、内外归属、正负半模可见域 | 04.01、05.01—05.07 | `G_PART_VISIBLE_AREA_POS/NEG`、各Feature拔模角场、`G_DRAFT_SIGNED_FIELD` |
| 派生分析域 | `UNDERCUT_EVIDENCE` | 用户主方向下内/外倒扣连通域、边界、深度见证 | 04.02—04.04 | `G_UNDERCUT_OUT/IN_REGION/AREA/COUNT`、`G_UNDERCUT_OUT/IN_DEPTH_MAX`、`G_UNDERCUT_DEPTH_WITNESS` |
| 派生分析域 | `DRAFT_SIGN_EVIDENCE` | 规则从带符号角度场筛出的零拔模和反拔模证据域 | 05.06/05.07 | `G_DRAFT_SIGNED_FIELD`；Hermes按阈值生成Finding证据集合 |
| 派生比较域 | `SYMMETRY_COMPARISON_SET` | 对称面/轴、两组壁厚域或筋网络、对应实例、未匹配实例 | 09.01/09.02 | `G_WALL_PAIR_THK_DIFF_MAX/MEAN`、`G_WALL_PAIR_AREA_MISMATCH`、`G_RIB_LAYOUT_COUNT_DIFF/POS_DEV_MAX` |
| 派生候选域 | `MICRO_FEATURE_CANDIDATE` | 小面、小边、小孔、小槽、小凸起及其来源Feature、CAD碎面过滤状态 | 12.08 | `G_FEATURE_MIN_SCALE`、`G_EDGE_LEN_MIN`、`G_FACE_AREA_MIN`、`G_FILLET_R_MIN`、`G_LOCAL_THK_MIN` |

### 6.1 全局、主体壁和通用过渡

| Feature/Scope | Geometric内部必须区分的区域 | 选择/配对/上下文语义 | 主要Check来源 |
| --- | --- | --- | --- |
| `PART` | 整件实体、有效表面、未分类表面 | 用户主出模方向；正/负半模及内外归属由Worker按统一算法计算并返回证据 | 01.01—01.04、04.01—04.04、05.06/05.07、12.08 |
| `WALL` | 外侧壁、内侧壁、有效厚度域 | 对向面选择、局部主壁分区 | 01.01—01.03、05.01/05.02 |
| `WALL_TRANSITION` | 厚侧、薄侧、过渡路径、过渡blend | 连接哪两个厚度域 | 01.10、06.06 |
| `GENERIC_FILLET` | 内凹blend、外凸blend、相邻母面、未倒圆的内/外尖边候选 | blend两端母面及凹凸属性；尖边按R=0/缺少圆角表达 | 06.01/06.02 |
| `THICK_MASS_CANDIDATE` | 厚肉实体域、背面对应区 | 来源Feature或交汇位置 | 01.04、01.06 |
| `DRAFT_SCOPE` | 所有有效侧壁、内外归属 | 用户主出模方向、成型侧、可见性；不自动产生方向候选 | 04类、05类 |

### 6.2 工程语义特征

| Feature | Geometric内部必须区分的区域 | 选择/配对语义 | 主要Check来源 |
| --- | --- | --- | --- |
| `RIB` | 两侧壁、根、顶、端、实体域、邻近主壁参考 | 连接主壁、筋间交汇、支撑BOSS | 01.05/01.06、05.03、06.03、09.02、11.02、12.06 |
| `BOSS` | 外壁、可选内壁、顶、根、根部blend、实体域、轴线、主壁参考 | 连接主壁、被筋支撑、最近有效外壁 | 01.07/01.08、05.04、06.04、11.02/11.04 |
| `SCREW_BOSS` | 继承BOSS；增加孔壁、孔底、孔底blend、顶环面 | 螺钉用途、孔底对应外壁、支撑筋集合 | 11.01及螺钉柱专属规则 |
| `HOLE` | 孔壁、开口、有效段、轴线；具体部位由通孔/盲孔/非圆开口子型决定 | 最近外壁、配对孔、附近筋 | 01.09、05.02、12.01—12.06、12.08 |
| `HOLE_THROUGH` | 孔壁、两端开口、有效成型段、轴线 | 最近外壁、配对孔、附近筋 | 12.01、12.04—12.06 |
| `HOLE_BLIND` | 孔壁、开口、孔底、有效段、轴线、对应外壁 | 孔底对应外壁、最近壁/孔/筋 | 01.09/12.02—12.06 |
| `OPENING_NONROUND` | 非圆开口边界、侧壁、有效通道、可选底面 | 最近外壁/筋；无等效直径规则时不套用L/D | 05.02、12.04/12.06、12.08 |
| `SNAP` | 悬臂梁段、固定根、钩部、侧壁、自由端/约定加载点 | 连接母壁；A类使用用户主方向，局部退出方向仅在补充输入后使用 | 05.05、06.05、11.06/11.07 |
| `LIP_OR_LEDGE` | 薄壁、台阶面、根部和相邻母壁 | 连接母壁、对向壁 | 11.08 |
| `CANTILEVER` | 梁段、固定根、自由端、中性路径和控制截面 | 连接母体 | 11.11；SNAP可作为其子型 |
| `SLOT_OR_DEEP_CAVITY` | 槽口、侧壁、底面、最窄截面路径 | 开口到底部、两侧壁配对 | 12.07 |
| `MICRO_FEATURE_CANDIDATE` | 小面、小边、小孔/槽/凸起候选域 | 来源Feature、CAD碎面过滤状态 | 12.08 |

### 6.3 Geometric内部必须实现的配对语义

| 配对语义 | 用途 | 结果证据要求 |
| --- | --- | --- |
| 局部主壁参考 | 筋、BOSS、圆角比例使用哪个局部主壁厚 | 返回被选主壁Faces、参考厚度及匹配质量 |
| 对应/对向表面 | 壁厚、孔底厚、背面映射的成对区域 | 返回两侧Faces和见证点/线 |
| 连接对象 | 根部、悬臂固定端、筋/BOSS与主壁连接 | 返回连接Faces/Edges和接触/连接尺度 |
| 交汇对象 | 筋交汇和复合厚肉候选 | 返回参与交汇的Feature列表和交汇域 |
| 支撑对象集合 | BOSS加强筋存在、数量及连接完整性 | 返回筋Feature列表；没有时返回空集合而不是不执行 |
| 最近有效对象 | BOSS到外壁、孔到壁、孔到孔、孔到筋 | 返回被选对象、见证点，并说明排除共边零距等无效候选的依据 |
| 对称对应对象 | 壁厚和筋布局比较 | 返回比较基准、对应实例和未匹配对象 |
| 成型方向绑定 | A类绑定用户主出模方向；局部方向只在补充输入后使用 | 返回用户来源、模型坐标向量、符号约定和context标识 |

## 7. A类44行：Check、目标区域和Geometric映射

表中“已有”指Excel已出现相应Check框架；“部分”指可复用现有Geometric或只覆盖某个子型；“新增”指当前Excel未表示。标为“共享”的行不应重复开发算法。

### 7.1 壁厚与局部厚肉

| 原编号 | 建议Check | Feature及Geometric内部目标语义 | 依赖Geometric | Excel状态 |
| --- | --- | --- | --- | --- |
| 01.01 | `C_WALL_THK_UNIFORMITY` | WALL厚度域＋未分类薄壁域 | `G_WALL_THK_LOCAL/MIN/MAX`、新增`G_WALL_THK_P05/P95`、`G_WALL_THK_GRAD_MAX` | 部分：已有厚度量，无均匀性Check |
| 01.02 | `C_WALL_THK_RANGE` / `BELOW_MIN` | WALL厚度域＋未分类域 | `G_WALL_THK_MIN`及位置/有效域 | 已有，共享范围Check |
| 01.03 | `C_WALL_THK_RANGE` / `ABOVE_MAX` | 同上 | `G_WALL_THK_MAX`及位置/超厚域 | 已有，共享范围Check |
| 01.04 | `C_LOCAL_THICK_MASS` | BOSS/筋根/交汇等候选实体域＋背面 | 新增`G_LOCAL_THICK_MASS_EQ_THK_MAX`、`G_LOCAL_THICK_MASS_VOLUME`、`G_LOCAL_THICK_MASS_SECTION_MAX` | 新增 |
| 01.05 | `C_RIB_THK_RATIO` | RIB根/侧壁＋内部选择的局部主壁参考 | `G_RIB_ROOT_THK`、`G_RIB_WALL_REF_THK`、局部最小/最大比 | 已有；补全区域选择和证据契约 |
| 01.06 | `C_RIB_INTERSECTION_THICK_MASS` | RIB交汇域＋局部主壁 | 新增`G_RIB_INTERSECTION_EQ_THK_MAX`、`G_RIB_INTERSECTION_VOLUME`、`G_RIB_WALL_REF_THK` | 新增 |
| 01.07 | `C_BOSS_WALL_THK` | 普通BOSS内/外壁＋主壁参考 | 新增`G_BOSS_WALL_THK_FIELD/MIN/MAX`、`G_BOSS_WALL_REF_THK` | 部分：只有螺钉柱子型 |
| 01.08 | `C_BOSS_ROOT_CONNECTION` | BOSS根、主壁、根blend、支撑筋关系 | 新增`G_BOSS_ROOT_THK_MAX`、`G_BOSS_ROOT_CONTACT_AREA`、`G_BOSS_ROOT_FILLET_R_MIN`、`G_BOSS_SUPPORT_RIB_COUNT` | 新增 |
| 01.09 | `C_HOLE_BOTTOM_THK` | 盲孔底＋内部配对的对应外壁 | `G_HOLE_BOTTOM_THK`/螺钉柱专用量、`G_HOLE_WALL_REF_THK` | 已有；与12.03共享 |
| 01.10 | `C_WALL_THK_TRANSITION` | 厚薄侧A/B＋过渡路径 | `G_WALL_TRANS_T1/T2/DT/LEN/STEP_COUNT`，建议补`G_WALL_TRANS_GRAD_MAX` | 已有 |

### 7.2 脱模方向、倒扣和拔模

| 原编号 | 建议Check | Feature及Geometric内部目标语义 | 依赖Geometric | Excel状态 |
| --- | --- | --- | --- | --- |
| 04.01 | `C_PART_DRAWABILITY_GIVEN_DIR` | PART有效表面＋用户提供的`F_TOOL_MAIN_DRAW_DIR` | 新增`G_PART_VISIBLE_AREA_POS/NEG`、`G_PART_UNDERCUT_AREA`、`G_PART_DRAFT_FAIL_AREA`、`G_PART_PROJECTED_AREA` | 新增；只评价输入方向，不搜索/推荐方向 |
| 04.02 | `C_UNDERCUT_OUT` | 外侧成型面＋用户主方向派生的外倒扣域 | 新增`G_UNDERCUT_OUT_REGION/AREA/COUNT`、`G_UNDERCUT_OUT_DEPTH_MAX` | 新增；方向Fact必需 |
| 04.03 | `C_UNDERCUT_IN` | 内腔成型面＋用户主方向派生的内倒扣域 | 新增`G_UNDERCUT_IN_REGION/AREA/COUNT`、`G_UNDERCUT_IN_DEPTH_MAX` | 新增；方向Fact必需 |
| 04.04 | `C_UNDERCUT_DEPTH` | 倒扣证据域＋用户主方向 | 新增`G_UNDERCUT_DEPTH_MAX`、`G_UNDERCUT_DEPTH_WITNESS`；结果回显输入方向 | 新增；主方向下的几何深度不等于机构行程 |
| 05.01 | `C_WALL_DRAFT_OUT` | WALL外侧壁＋用户主方向 | `G_WALL_OUT_DRAFT_FIELD/MIN`；现`G_WALL_OUT_DRAFT`保留为汇总 | 已有框架；补角度场和面证据 |
| 05.02 | `C_WALL_DRAFT_IN` | WALL内侧壁/一般孔壁＋用户主方向 | `G_WALL_IN_DRAFT_FIELD/MIN`、按孔时`G_HOLE_DRAFT_FIELD/MIN` | 已有框架；不得以孔轴替代方向 |
| 05.03 | `C_RIB_DRAFT` | RIB两侧壁＋用户主方向 | `G_RIB_DRAFT_SIDE_A/B_FIELD/MIN`；现`G_RIB_DRAFT`作汇总 | 已有框架 |
| 05.04 | `C_BOSS_DRAFT` | BOSS或SCREW_BOSS内/外壁＋用户主方向 | `G_BOSS_DRAFT_OUT/IN_FIELD/MIN`；螺钉柱复用专用ID | 已有框架；轴线只作一致性证据 |
| 05.05 | `C_SNAP_DRAFT` | SNAP侧壁/钩部＋用户主方向 | 新增`G_SNAP_DRAFT_FIELD/MIN`、`G_SNAP_HOOK_UNDERCUT_DEPTH` | 新增；侧抽/强脱方向需另行输入后检查 |
| 05.06 | `C_DRAFT_SIGN` / `ZERO_DRAFT` | 全部有效侧壁；按用户主方向得到零拔模规则派生域 | 新增通用`G_DRAFT_SIGNED_FIELD`；各Feature已有角度场可复用 | 部分：量可复用，无Check |
| 05.07 | `C_DRAFT_SIGN` / `REVERSE_DRAFT` | 已归属成型侧的侧壁；按用户主方向得到反拔模规则派生域 | `G_DRAFT_SIGNED_FIELD`＋可见性/遮挡量 | 部分：量可复用，无Check |

### 7.3 圆角和过渡

| 原编号 | 建议Check | Feature及Geometric内部目标语义 | 依赖Geometric | Excel状态 |
| --- | --- | --- | --- | --- |
| 06.01 | `C_PRODUCT_FILLET_IN` | 通用凹blend＋相邻母面 | `G_WALL_IN_FILLET_R_MIN`、新增曲率/拟合误差 | 已有框架 |
| 06.02 | `C_PRODUCT_FILLET_OUT` | 通用凸blend＋相邻母面 | 新增`G_WALL_OUT_FILLET_R_MIN`、曲率/拟合误差、局部厚度 | 部分：现表只有内圆角 |
| 06.03 | `C_RIB_ROOT_FILLET` | RIB根blend＋主壁参考 | `G_RIB_ROOT_FILLET_R`、`G_RIB_WALL_REF_THK` | 已有框架 |
| 06.04 | `C_BOSS_ROOT_FILLET` | BOSS外根blend＋主壁参考 | `G_BOSS_ROOT_FILLET_R`、`G_BOSS_ROOT_REF_THK`；螺钉柱用更具体ID | 已有；修正螺钉柱TOP/ROOT命名 |
| 06.05 | `C_SNAP_ROOT_FILLET` | SNAP固定根blend＋梁/母壁 | 新增`G_SNAP_ROOT_FILLET_R_MIN`、`G_SNAP_ROOT_THK` | 新增 |
| 06.06 | `C_THK_TRANSITION_FILLET` | WALL厚薄过渡blend＋两侧厚度域 | 新增`G_WALL_TRANS_FILLET_R_MIN`、连续性等级；复用T1/T2 | 新增 |

### 7.4 对称、BOSS、卡扣和悬臂

| 原编号 | 建议Check | Feature及Geometric内部目标语义 | 依赖Geometric | Excel状态 |
| --- | --- | --- | --- | --- |
| 09.01 | `C_WALL_THK_ASYMMETRY` | 两组壁厚域＋确认的对称/比较映射 | 新增`G_WALL_PAIR_THK_DIFF_MAX/MEAN`、`G_WALL_PAIR_AREA_MISMATCH` | 新增；需比较基准 |
| 09.02 | `C_RIB_LAYOUT_ASYMMETRY` | RIB网络＋Geometric内部建立的对称对应 | 新增`G_RIB_LAYOUT_COUNT_DIFF`、`G_RIB_LAYOUT_POS_DEV_MAX`、长度/方向差 | 新增；需比较基准 |
| 11.01 | `C_SCREW_BOSS_WALL_THK` | SCREW_BOSS内/外壁＋主壁参考 | `G_SCREW_BOSS_WALL_THK_FIELD/MIN/MAX`、`G_SCREW_BOSS_WALL_REF_THK` | 已有；补MIN和场 |
| 11.02 | `C_SCREW_BOSS_RIB_SUPPORT` | BOSS＋支撑筋集合＋主壁连接 | 新增`G_BOSS_SUPPORT_RIB_COUNT`、`G_BOSS_SUPPORT_RIB_THK/HEIGHT`、连接长度、根R | 新增；支持数量0 |
| 11.04 | `C_BOSS_TO_OUTER_WALL_CLEARANCE` | BOSS外壁/根＋有效外壁配对 | 新增`G_BOSS_TO_OUTER_WALL_MIN_DIST`及见证点；可复用现`STEEL_THK`底层距离 | 部分：不得直接判薄钢 |
| 11.06 | `C_SNAP_CANTILEVER_LEN` | SNAP固定根、梁中性路径、自由端/加载点 | 新增`G_SNAP_CANTILEVER_LEN`、`G_SNAP_BEAM_THK_MIN/WIDTH` | 新增 |
| 11.07 | `C_SNAP_ROOT` | SNAP固定根和过渡域 | 新增`G_SNAP_ROOT_THK`、`G_SNAP_ROOT_FILLET_R_MIN`、`G_SNAP_ROOT_TRANS_LEN` | 新增 |
| 11.08 | `C_LIP_WALL_THK` | LIP/LEDGE薄壁、台阶、邻近母壁 | 新增`G_LIP_WALL_THK_FIELD/MIN/MAX`、`G_LIP_STEP_HEIGHT` | 新增 |
| 11.11 | `C_CANTILEVER_SLENDERNESS` | 通用CANTILEVER根、梁段、自由端和控制截面 | 新增`G_CANTILEVER_LEN`、`G_CANTILEVER_SECTION_THK_MIN/WIDTH`、`G_CANTILEVER_SLENDERNESS` | 新增 |

### 7.5 孔、槽和微小特征

| 原编号 | 建议Check | Feature及Geometric内部目标语义 | 依赖Geometric | Excel状态 |
| --- | --- | --- | --- | --- |
| 12.01 | `C_HOLE_THROUGH_LD` | 通孔有效段、两开口、轴线 | 新增`G_HOLE_EFFECTIVE_LEN`、`G_HOLE_DIA_EFFECTIVE_MIN`、`G_HOLE_LD_RATIO` | 新增 |
| 12.02 | `C_HOLE_BLIND_LD` | 盲孔有效段、开口、孔底、轴线 | 通用`G_HOLE_EFFECTIVE_DEPTH`、`G_HOLE_DIA_EFFECTIVE_MIN`、`G_HOLE_LD_RATIO` | 部分：现有只覆盖螺钉柱孔 |
| 12.03 | `C_HOLE_BOTTOM_THK` | 与01.09相同 | 与01.09共享同一Measurement | 已有；来源别名，不重复执行 |
| 12.04 | `C_HOLE_CLEARANCE` / `TO_OUTER_WALL` | 孔边界＋有效外壁配对 | `G_HOLE_TO_EDGE_MIN_DIST`、见证点/边界；保留孔径和壁厚上下文 | 已有框架；补Region配对规则 |
| 12.05 | `C_HOLE_CLEARANCE` / `TO_HOLE` | 两个孔实例＋真实边界 | `G_HOLE_TO_HOLE_MIN_DIST`、轴距、有效孔径及见证点 | 已有框架 |
| 12.06 | `C_HOLE_TO_RIB_CLEARANCE` | 孔壁＋RIB壁/根的有效配对 | 新增`G_HOLE_TO_RIB_MIN_DIST`、目标部位和见证点 | 新增 |
| 12.07 | `C_SLOT_ASPECT_RATIO` | SLOT开口、底、两侧壁、最窄截面 | 新增`G_SLOT_DEPTH_EFFECTIVE`、`G_SLOT_WIDTH_MIN`、`G_SLOT_HW_RATIO`、截面证据 | 新增 |
| 12.08 | `C_MIN_FEATURE_SIZE` | 微小孔/槽/凸起/边/面候选＋来源Feature | 新增`G_FEATURE_MIN_SCALE`、`G_EDGE_LEN_MIN`、`G_FACE_AREA_MIN`、`G_FILLET_R_MIN`、`G_LOCAL_THK_MIN` | 新增；过滤CAD碎面 |

## 8. 推荐落地顺序

1. 先补Geometric契约：明确螺钉柱各Geometric内部使用的外壁、孔壁、孔底、外根、参考主壁和对象配对，并定义结果中的拓扑证据；不新增独立Region/Relation协议。
2. 用一个螺钉柱样件跑通：识别区域和关系→只请求所需Geometric→Factor选Rule→产生Finding及三维证据。
3. 修正螺钉柱三个现有问题：壁厚补MIN/场、圆角拆Check并修正TOP/ROOT、孔深第二分支阈值冲突。
4. 按共享几何能力扩展到普通BOSS、筋、主体壁和孔，先完成已有规则页对应的16行。
5. 再补倒扣、卡扣、对称、止口、悬臂、槽和微小特征等21个当前缺失来源行。
6. 最后将A类44个来源行映射到41个执行Check，并建立自动校验：每个Check至少有目标Feature/Scope、Geometric依赖、Factor依赖和适用能力声明；每个Geometric至少有内部区域/配对定义及证据返回契约。

首版不必一次冻结所有阈值，但必须先冻结Geometric ID的区域/配对语义、量测定义、聚合口径和证据格式。阈值可以通过Rule版本迭代；同一Geometric在不同实现中选择不同区域或参考对象，会让后续规则、缓存和证据失去稳定基础。
