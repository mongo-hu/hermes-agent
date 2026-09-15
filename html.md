# 要求

当前 DFM Agent 主流程：

```text
3D STEP + 对应的 2D PDF 图纸
  -> OCCT 几何计算 + OCR 文本提取
  -> 生成 report、证据图、3D/标量场等产物
  -> 生成最终人类可读报告
```

当前最终报告是硬编码 PPT。现在需要用已经调试好的 HTML pipeline 替换 PPT 生成模块。

相关路径：

```text
DFM 主 pipeline：tools/dfm
HTML pipeline：DFM-HTML
HTML 脚本：DFM-HTML/generate_html_report.py
HTML 说明：DFM-HTML/README.md

HTML 输入：
  DFM-HTML/example/generated/llm_content.jsonl
  DFM-HTML/example/generated/runtime_data.jsonl

HTML 输出：
  DFM-HTML/example/generated/output/report.html
```

---

# 实施方案

## 1. 改造边界

本次只替换最终报告展示模块：

```text
旧：现有 DFM 生成物 -> PPT renderer -> dfm_report.pptx
新：现有 DFM 生成物 -> 两份 JSONL -> HTML generator -> report.html
```

以下主 pipeline 环节全部保持不变：

- STEP/PDF 输入和项目管理；
- OCR 与当前 Agent 的图纸语义解释；
- OCCT 几何计算；
- 规则计算与 Evaluation；
- 证据图、3D scene、scalar field 和几何溯源生成；
- `dfm_report.json`；
- `dfm_report.md`。

`dfm_report.json` 和 `dfm_report.md` 本来就是 DFM 主 pipeline 的生成物。本方案不重新生成或改变它们，只是不再生成 `dfm_report.pptx`，改为生成 `report.html`。

## 2. HTML 数据来源

HTML 所需资源全部由现有 DFM pipeline 提供，不新增任何 OCR、几何计算、规则计算或证据渲染。

| HTML runtime 内容 | 现有 DFM 来源 |
|---|---|
| `report` | `dfm_report.json` |
| `drawing_semantics.observations` | 第一次 Hermes 图纸理解生成的 `drawing_observations` JSONL |
| 问题证据图片 | `evidence_records` 和 `evidence_image` artifacts |
| `resources.evidence_root` | 当前 run 的 artifacts 目录 |
| `resources.scene_path` | `render_scene` artifact |
| `resources.scalar_fields` | `scalar_field` artifacts |
| `resources.evidence_geometry_path` | `evidence_geometry` artifact |
| `resources.drawing_pdf_path` | 当前项目的 PDF drawing input |
| `resources.rule_library_path` | 当前 plan 固定的规则/ontology snapshot |
| `display.process` | `PlanRecord.process` |
| `display.rule_scope` | `PlanRecord.scope_id/scope_version` |
| `model_metrics` | measurements 和 `dfm_report.json.stats` |

生成 `runtime_data.jsonl` 只是把这些已有数据和 artifact 路径写成 HTML pipeline 已定义的输入格式，不是重新计算数据。

scalar field 应读取 artifact JSON 中的 `metric_id` 来区分壁厚场和拔模场，不能依赖 artifact 列表顺序。

## 3. 改造后的完整流程

```text
STEP + PDF
  -> 原有 OCR
  -> 当前 Hermes Agent 一次性整理全部图纸事实并持久化 drawing_observations JSONL
  -> 原有 OCCT / Evaluation / Evidence
  -> 原有 dfm_report.json 和 dfm_report.md
  -> Runtime adapter 合并 drawing_observations 与 2D/3D 产物，生成 runtime_data.jsonl
  -> run 进入 reporting / report_editing / 98%（尚未成功）
  -> 当前 Hermes Agent 通过 report_context 获取内联 observations/runtime 并生成 llm_content.jsonl
  -> 调用现有 HTML generator
  -> report.html
  -> 将 report.html 登记为本次 run 的正式 artifact
  -> run 进入 succeeded / complete / 100%
```

该过程属于完整的 DFM Agent pipeline，不需要用户在流程结束后手动执行仓库外脚本。

当前 DFM worker 只负责确定性计算，不在 worker 内另起 LLM 客户端。图纸 OCR 只在 Discovery 阶段由当前 Hermes 会话模型解释一次，并持久化为 `drawing_observations` JSONL。Runtime adapter 只复制这份已验证语义结果并映射已有 2D/3D artifacts；报告阶段的当前 Agent 基于该结构化数据生成 `llm_content.jsonl`，不重新读取或解释 OCR。HTML renderer 仍只消费 `llm_content.jsonl` 和 `runtime_data.jsonl`。

## 4. 将 HTML 模块放进主 pipeline

将已调试的 `DFM-HTML` 代码放入主 DFM reporting 模块：

```text
tools/dfm/reporting/html/
├─ __init__.py
├─ generator.py                 # 主 pipeline 渲染入口
├─ template.py                  # 现有 generate_html_report.py
├─ runtime_adapter.py           # 现有 DFM artifacts -> runtime_data.jsonl
└─ vendor/
   ├─ three.r128.min.js
   └─ OrbitControls.r128.js
```

保留现有两份 JSONL 契约和 HTML 页面实现，不重写已经调试好的模板。

`DFM-HTML/generate_html_report.py` 可以保留为示例/独立调试入口，但它应调用主 reporting 包中的同一实现，避免产生两套 HTML 代码。

主 pipeline 直接 import generator，不通过 subprocess 调用仓库根目录脚本。

## 5. 生成 runtime_data.jsonl

`runtime_adapter.py` 接收：

```text
project_dir
run_id
PlanRecord
本次 run 的 ArtifactRecord 列表
当前项目的 InputRecord 列表
当前 drawing input 对应的 `drawing_observations` ArtifactRecord
本次 plan 的 frozen Observation IDs
dfm_report.json 内容
```

输出：

```text
runs/<run_id>/artifacts/runtime_data.jsonl
```

它只执行以下映射：

1. 将现有 `dfm_report.json` 写入 `runtime.report`；
2. 从 PlanRecord 写入 process 和 rule scope；
3. 从 artifacts 定位 scene、scalar fields、evidence geometry 和证据图；
4. 从本次 plan 的 input IDs 定位原始 PDF；
5. 读取第一次 Hermes 已持久化的 `drawing_observations` JSONL，并按本次 plan 的 DiscoverySnapshot Observation IDs 固定版本；
6. 将完整结构化图纸语义写入 `runtime.drawing_semantics.observations`，同时记录原 JSONL 的 artifact ID 和路径；
7. 引用本次 plan 已固定的规则 snapshot；
8. 从 measurements/report stats 整理 model metrics；
9. 使用相对当前 artifacts 目录的路径写出单行 JSONL。

这一层不修改任何原始 DFM artifact，也不进行任何计算。

生成后登记为：

```text
kind: report_html_runtime
media_type: application/x-ndjson
```

## 6. 由当前 Agent 生成 llm_content.jsonl 并渲染 HTML

确定性 worker 完成后，run 进入 `reporting` 而不是 `succeeded`。当前 Hermes Agent 调用 `report_context`，直接读取其返回的完整内联 Runtime；不再依赖 terminal/read-file 打开 `runtime_data.jsonl`。其中 `drawing_semantics.observations` 是 Discovery 阶段第一次 Hermes OCR 理解生成并经过服务校验、持久化的唯一图纸语义来源；`runtime.report` 和 `resources` 则来自后续 2D/3D 分析产物。报告阶段不再调用 `drawing_context`，也不重新解释 OCR，而是据此生成 `dfm-html-llm/v1` 对象：

```text
part
issues[]
conclusion
```

沿用现有 HTML 契约：

- `llm.issues[].issue_id` 与 `runtime.report.issues[].id` 集合完全一致；
- 最后一次 LLM 是报告编辑器，不是字段复制器：负责整理零件名称、通用公差、技术 Notes和问题说明，综合 2D/3D 结果总结风险、排序优化建议，并按用户语言本地化报告文案；
- 每条技术 Note 在 Discovery 阶段单独保存为 `global_note` observation，报告阶段必须完整覆盖到 `part.technical_note`；可翻译、分类和合并组织，但不得遗漏或改变含义；
- 零件号、标准号、规则 ID、数值、运算符、单位和引擎版本必须原样保留；不得把未评估的检查项总结为已通过；
- actual、expected、operator、rule ID、severity 和证据路径只来自 runtime；
- 没有图纸依据的公差或技术要求填写 `null`；
- 不新增主 pipeline 中不存在的问题。

Agent 通过现有 `dfm_analysis` 工具的 `render_html` action 提交该对象。服务端校验 run 和 runtime artifact，写入 `llm_content.jsonl`，调用 HTML generator，并把 `llm_content.jsonl` 与 `report.html` 登记到同一个 run；只有这些步骤全部成功后，run 才原子地从 `reporting/98%` 转为 `succeeded/100%`。这里没有新增 model tool，只扩展现有 DFM 工具的报告阶段 action。

## 7. 具体代码改动

### `tools/dfm/reporting/result_assembler.py`

保持现有 JSON/Markdown 生成逻辑不变。

删除默认流程中的 PPT 调用：

```python
if pptx_available():
    render_default_reports(...)
```

其余 JSON/Markdown 装配逻辑保持不变。

### `tools/dfm/runtime/jobs.py`

在 JSON/Markdown 和 evidence artifacts 完成后生成 `runtime_data.jsonl` 并登记 `report_html_runtime`。HTML-capable run 随后进入 `reporting/report_editing/98%`，worker 到此结束且不调用 LLM；此状态不是 pipeline 成功。

### `tools/dfm/service.py`

增加报告阶段的 `report_context` 和 `render_html` action：前者等待确定性分析并把完整 Runtime 内联交给当前 Agent，后者接收 Agent 生成的 `llm_content`、调用 renderer、登记产物并将 run 标记成功。

### `tools/dfm_tool.py`

在既有 `dfm_analysis` action enum 和参数 schema 中加入 `report_context`、`render_html`、`wait_seconds` 与 `llm_content`。

### `skills/manufacturing/dfm-analysis/SKILL.md`

要求当前 Agent 在 Discovery 阶段读取所有 drawing pages，一次性持久化完整图纸事实；报告阶段只读取 `runtime.drawing_semantics.observations` 和确定性 Runtime，生成 `llm_content`、调用 `render_html`，并将 `report.html` 作为主要可读报告。

## 8. 最终 artifacts

成功执行后，本次 run 包含：

```text
原有且不变：
  measurements
  evaluations
  render_scene
  scalar_field
  evidence_geometry
  evidence_records
  evidence_image
  dfm_report.json
  dfm_report.md

HTML 接入新增：
  runtime_data.jsonl
  llm_content.jsonl
  report.html

默认不再生成：
  dfm_report.pptx
```

新增 artifact kinds：

```text
report_html_runtime
report_html_llm
report_html
```

`report_html` 的媒体类型为 `text/html; charset=utf-8`。

## 9. 测试

### Runtime adapter

- 使用真实 ArtifactRecord 列表生成 `runtime_data.jsonl`；
- 验证 report、scene、两个 scalar field、geometry、PDF、rule snapshot 和证据图路径正确；
- 验证 adapter 没有修改或重新生成任何已有 DFM 数据。

### HTML generator

测试在 `tmp_path` 中构造最小、完整的 `dfm-html-llm/v1` 和 `dfm-html-runtime/v1` 契约及对应资源，再通过主 reporting 包的 `render_html_report` 渲染。这使测试不依赖未跟踪的 `DFM-HTML/example` 大型样本目录。

验证 HTML 存在、契约 issue ID 已渲染、中文综合评估文案存在且 Three.js 资源已内嵌。

### 主 pipeline 集成

完整执行：

```text
STEP + PDF
-> discover/OCR
-> Hermes 完整语义整理并持久化 drawing_observations JSONL
-> plan/start
-> reporting/report_context
-> Hermes 总结/render_html
-> succeeded/result
```

验证：

- `dfm_report.json` 和 `dfm_report.md` 与改造前一致；
- HTML 生成前 run 保持 `reporting/98%`，`result` 不得成功；
- 不再生成 `report_presentation`；
- 生成 `report_html_runtime`、`report_html_llm` 和 `report_html`；
- HTML issue IDs 与 `dfm_report.json` 完全一致；
- Runtime 中的图纸语义与本次 plan 固定的 `drawing_observations` 一致，报告阶段不读取 OCR；
- HTML 中的工程数值和资源全部来自原有 DFM artifacts；
- HTML 可以离线打开并正常使用 3D、证据图、PDF 和溯源功能。

## 10. 验收标准

1. OCR 提取、OCCT、Evaluation、Evidence、JSON 和 Markdown 逻辑不变；Hermes 只在 Discovery 阶段解释一次 OCR。
2. HTML 所需全部资源来自当前 DFM run，不进行重复计算。
3. 当前 Hermes Agent 基于已持久化的 `drawing_observations` 与 Runtime 生成 `llm_content.jsonl`，完成最终的本地化、总结、风险解释和优化建议；worker 不调用第二个模型，adapter 不代替 LLM 总结。
4. HTML generator 正式进入 `tools/dfm/reporting`，不依赖人工执行外部脚本。
5. `report.html` 成为主 pipeline 的最终人类可读报告。
6. 默认不再生成 `dfm_report.pptx`。
7. HTML 的工程事实与 `dfm_report.json` 完全一致。
8. HTML-capable run 只有在 `report.html` 校验并登记后才能进入 `succeeded/100%`。
