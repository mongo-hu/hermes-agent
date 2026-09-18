# Mold / Agent DFM 契约对齐

> 本文记录 2026-09-17 的 Schema 2 对齐状态。最新 Schema 3 以
> [规则库设计](dfm-rule-catalog-database-design.md) 为准：发布快照与管理库的 JSON 字段
> 同名使用 `_json` 后缀，例如 `properties_json`、`qualifiers_json`、
> `conditions_json`、`acceptance_criteria_json`；Agent 已适配，Mold 发布器和数据迁移待落地。
> 下文的 Schema 2 流程与验证记录不代表 Schema 3 的当前发布状态。

本次以 `dfm-rule-catalog-database-design.md` 与已更新的 Mold 代码为依据，重点修改 Agent。
Mold 已实现 Geometric、比较对象原文、属性只读白名单、来源码及几何条件校验；未重复覆盖其实现。

## 公共字段

| 层级 | 字段与含义 |
| --- | --- |
| Check | 业务指标；通过 `APPLIES_TO_FEATURE` 关联 Feature，通过 `USES_OPERAND` 关联 Geometric |
| Geometric | 技术指标概念；`worker_geometric_id/quantity_id/dimension/canonical_unit` 是运行绑定 |
| Operand | `USES_OPERAND.qualifiers` 中的 `alias/aggregation/required/operand_text`；不新增关系表 |
| 几何条件 | `geometric_id` 是当前 Check 的 Operand alias，不是 Geometric Concept ID |
| OCCT | 继续使用原生 `metric_id/quantity_id`；不传业务阈值，不暴露 C++ 函数名 |
| Evidence | Feature、运行时 Region、geometry_refs、field_refs、输入及拓扑快照身份继续保留 |

当时 Agent 的 Snapshot Schema 与 Mold 发布 Schema 内容一致；Schema 3 扩展目前仅在 Agent 侧落地，
须待 Mold 发布器更新后重新进行跨仓契约验证。
本地安装再校验白名单、关系端点、Alias、原文、来源策略、有限几何值和标准单位。校验失败不会替换已安装数据库。

## 执行流程

1. 安装完整发布包，校验内容哈希并固定快照身份。
2. 依据确认 Factor 筛选候选规则；几何条件尚未计算，不提前判 false。
3. 将 Geometric 绑定到已有的 Metric/Quantity Operation，并把比较对象原文带入目标解析。
4. 根据实际 Discovery 的 Feature/Region 绑定计算范围，同一 Metric/Region 目标复用一个 Operation。
5. Plan 固定候选规则及可选 `RuleBinding.rule_selection`；OCCT 仍只收到客观几何任务。
6. Measurement 返回后按 Check 实例匹配几何条件，应用默认规则、优先级及具体度，冲突显式报错。
7. 对选中规则评价并保存完整 Measurement/Operand/区域证据。

没有几何条件的规则仍可在计划阶段选定。`rule_selection` 为空时不序列化该字段，避免改变历史 Binding 的哈希输入。
规则条件使用的 Operand 即使没有出现在算术表达式里，也会被测量并进入证据链。规则切换不改变 OCCT 算法参数。

## 目标解析边界

本体没有 Region Type，不等于可以丢弃运行时几何范围。本次解析器仅支持明确的比较对象语义与
既有 Discovery 角色（普通主体、柱壁、内外壁、根部、顶底面、孔等），不是任意自然语言执行器。
未知原文、多个可能区域、混合角色会阻止执行，错误包含 Check、Operand alias、原文和候选区域。

“相邻主体壁厚度”必须由 Discovery 提供：

- 螺钉柱自身明确的 `adjacent_main_wall` 局部 Region；或
- Feature.relationships 中的 `attached_to/adjacent_to`、`target_ref`，以及明确的局部 `region_refs`。

关联必须落在同一输入模型中。只有主体壁 Feature、没有局部区域关联时，不会用整个主体壁或整件壁厚替代。
这项约束不代表当前 OCCT 二进制已实现螺钉柱/邻近壁识别；新增算法仍需独立实现和认证 Capability。

## 版本与验证

- Schema 数字仍为 2，但新旧字段结构不同，不能只按 `schema_version` 判兼容。
- 随仓示例更新为 `ontology.injection.default@1.3.0`，规则集 `injection.default@1.2.0`，使用新的内容哈希。
- 示例用于默认工作流和回归，不代替不可修改的 `V2.2.xlsx` 业务权威来源。
- 旧示例作为测试夹具保存。旧 Schema 1/2 Metric/Region 快照保留兼容读取，不自动改写旧快照、已有 Plan 或运行结果。
- 本次没有执行 Mold 数据库迁移、业务数据导入、生产发布或替换 OCCT 二进制。

定向验证：

```text
bash scripts/run_tests.sh tests/tools/dfm/test_catalog_alignment.py tests/tools/dfm/test_ontology_store.py tests/tools/dfm/test_service.py tests/tools/dfm/test_multi_measurement_evaluation.py -j 4 -q
```

覆盖安装失败原子性、新旧快照、跨仓 Schema、几何条件、条件专用 Operand、多实例独立选规、目标复用和邻近局部区域约束。

2026-09-17 验证结果：

- 定向回归（另含同步和客观结果缓存）：69 项通过。
- 完整 DFM / CLI 回归：279 项通过、3 项失败；静态检查和差异空白检查通过。
- 真实 OCCT 完成识别、测量、规则评价、区域证据和报告 Runtime 生成，进入 `reporting / report_editing`，等待 Agent 编写报告。
  原生测试已修正过时的 `succeeded` 等待条件；没有改动运行状态机，也不把待编写的 HTML 报告算作已完成。
- 剩余失败：2 项 HTML 编辑器测试缺少 Playwright 依赖；1 项 CLI 测试在清空环境变量的 Windows 测试环境中无法解析用户目录。
- Mold 侧完成代码和公共 Schema 核对；本次未执行其 Django 数据库集成测试，不等同于生产发布验证。
