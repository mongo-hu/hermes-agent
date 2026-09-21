# DFM 2D 语义裁剪 + 多模态提取算法迁移说明

> 用途：在拉取新版 Hermes Agent 代码后，重新落地本分支已经验证的 2D 图纸算法。
>
> 记录日期：2026-09-21
>
> 当前实现分支：`dfm-2D-crop`
>
> 实验原型：`D:\hermes_deployment\hermes-agent-dfm-hermes-agent2\experiments\dfm_visual_extraction\scripts\semantic_crop_pipeline.py`

## 1. 目标与边界

目标是让 DFM 对 PDF/PNG/JPG 工程图执行两阶段多模态分析：

1. 将完整图纸的所有页面以低分辨率图像一次性发送给视觉模型，由模型规划少量完整语义区域；
2. 程序校验、补边、吸附并高清渲染这些区域，再一次性发送所有高清区域给视觉模型，提取结构化图纸事实。

关键架构约束：

- 不配置第二套视觉 API、模型或密钥；
- 两次内部视觉请求复用当前主 Hermes Agent 的 provider、model、base URL、API key 和 API mode；
- 两次视觉请求不进入主 Agent 对话循环，不携带主对话历史，也不把页面图片或中间模型输出写入主 Agent 上下文；
- 主 Agent 只看到 `dfm_analysis(action="discover")` 的结构化最终结果或明确错误；
- 不打开浏览器分析 PDF，不调用通用 `vision_analyze`，不依赖 OCR；
- 坐标、区域 ID、Observation ID、revision 和持久化均由 DFM 服务校验；模型只能提出候选内容；
- 空裁剪计划必须失败关闭，禁止把图纸静默当成“没有内容”。

这里的“后台”是指 **DFM 工具调用内部的模型 side-call**。它不是独立队列任务；
对用户界面而言只显示一个正在执行的 DFM Analysis 工具调用。混合 CAD + 图纸项目中，
完整 2D 分支会与 3D 几何发现并行，但 2D 内部的两次请求仍因数据依赖而顺序执行。

## 2. 总体数据流

```text
用户附件（PDF/PNG/JPG，可同时带 STEP）
  │
  ├─ dfm_project(create)
  ├─ dfm_project(add_input) 逐个登记所有附件
  │    └─ PDF/PNG/JPG -> InputRecord(kind="drawing")
  │
  └─ dfm_analysis(discover)
       │
       ├─ 确定性预处理
       │    ├─ PyMuPDF 打开文档
       │    ├─ 获取页数/页面尺寸
       │    └─ 所有页面以 72 DPI 渲染到内存 PNG
       │
       ├─ 并行分支 A：2D 图纸语义理解
       │    ├─ 多模态调用 1：全页语义裁剪规划
       │    ├─ 程序校验、补边、吸附并渲染 200 DPI 裁剪
       │    ├─ 多模态调用 2：高清区域事实提取
       │    └─ 校验 source_region_refs 并持久化区域与 Observation
       │
       ├─ 并行分支 B：STEP/Parasolid 3D 几何发现
       │
       └─ 两分支汇合后继续 clarification、fusion 和 snapshot
            ├─ clarification（需要用户确认时）
            ├─ 2D/3D fusion（仍可由主 Agent 提议）
            └─ freeze discovery snapshot
```

## 3. 为什么必须是“两次调用”

一次把完整高分辨率 PDF 全部发送给模型有三个问题：上下文过大、重要小字不清晰、配置页噪声容易污染结果。两阶段方案把任务拆开：

- 第一次只做视觉导航，不抄写事实：全局判断哪些区域值得保留；
- 程序根据坐标生成高清图：把 Notes、标题栏、BOM、尺寸和制造标注放大；
- 第二次只做事实整理：每条结论必须引用一个或多个稳定 region ID。

实验版以 `all-pages-single-call` 和 `all-crops-single-call` 运行，避免逐页调用造成跨页信息丢失及调用数量膨胀。实验记录曾得到 2 次模型调用、10 个裁剪区，并显著提升严格事实召回率；迁移后应继续保留“全页一次 + 全裁剪一次”的调用策略。

## 4. 第一阶段：全页裁剪规划

### 4.1 页面渲染

实现位置：

- `tools/dfm/drawing_pipeline/core_pipeline.py::render_overviews`
- `tools/dfm/drawing_pipeline/interface.py::render_overviews`
- `tools/dfm/analyzers/drawing.py::DrawingAnalyzer.render_overviews`

规则：

- PDF 使用 PyMuPDF；PNG/JPG 通过统一文档打开逻辑处理；
- 默认最大 50 页；
- 预览分辨率为 72 DPI；
- 返回 `DrawingImage(page, pixel_width, pixel_height, png_bytes)`；
- 图像在内存中编码为 `data:image/png;base64,...`，不要求写临时文件。

发送给模型的 content 顺序必须稳定：

```json
[
  {"type": "text", "text": "<裁剪规划 prompt + contract>"},
  {"type": "text", "text": "PAGE 1 OF 2"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
  {"type": "text", "text": "PAGE 2 OF 2"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]
```

### 4.2 规划 prompt 的职责

模型应保留：

- 标题栏：零件身份、图号、版本、单位、比例、页数、通用公差；
- 所有技术 Notes，包含每个编号、字母和连字符子项；
- 材料表/BOM，并保留表头、部件名与材料行关系；
- 产品总成页的明确尺寸、公差、MAX、括号参考尺寸；
- 制造、位置及装配标注；
- 解释上述内容所需的箭头、引出线、视图名和表头。

模型应排除：

- 目录；
- 修订历史；
- 料号图例；
- 没有语义标注的纯几何视图；
- 配置页/配置矩阵里的料号、颜色、状态及配置专用尺寸。

不确定但可能重要的区域使用 `keep_uncertain`，不能静默丢弃。

### 4.3 裁剪输出契约

模型只返回 JSON，不调用工具：

```json
{
  "regions": [
    {
      "page": 1,
      "region_id": "p1_notes",
      "type": "notes",
      "bbox_1000": [120, 80, 760, 520],
      "decision": "keep",
      "reason": "Complete numbered notes block"
    }
  ]
}
```

坐标系：页面左上角为原点，四个整数 `[left, top, right, bottom]`，范围 0–1000，与实际像素和 PDF point 无关。

允许的 region type：

- `notes`
- `title_block`
- `materials_bom`
- `assembly_dimensions`
- `manufacturing_callouts`
- `other`

允许的 decision：`keep`、`keep_uncertain`。

## 5. 程序化区域校验与高清裁剪

实现位置：`tools/dfm/drawing_pipeline/core_pipeline.py`。

### 5.1 强制校验

`_validate_regions` 应满足：

- 输入必须是 list；
- 必须至少包含 1 个区域，最多 48 个；
- 每项必须是 object；
- 页码必须在文档范围内；
- bbox 必须恰好 4 个数且满足 `0 <= left < right <= 1000`、`0 <= top < bottom <= 1000`；
- 四边默认增加 24 个归一化坐标单位的 padding，并裁到 0–1000；
- 补边后的宽、高均不得小于 35；
- 非法 region type 降为 `other`；
- region ID 只保留字母、数字、下划线和连字符，最大 80 字符；
- 重复 ID 追加序号；
- 空数组必须报错，不能产生 `available_region_ids: []`。

当前生产实现的错误语义为 `drawing_crop_plan_empty` 或由 drawing interface 包装成 `drawing_crop_plan_invalid`。

### 5.2 标题栏去重

`_deduplicate_document_regions` 只保留最早页面的 `title_block`，减少跨页重复标题栏占用视觉上下文。注意：如果新版业务需要逐页比例或逐页版本，必须先调整规则，不能盲目删除该去重。

### 5.3 PDF 文本块吸附

`_snap_to_text` 的目的不是 OCR，而是利用 PDF 自带文本块边界避免模型 bbox 截断文字：

1. 将 `bbox_1000` 转成 PDF page coordinates；
2. 读取 `page.get_text("blocks")` 中非空文本块；
3. 若文本块与当前语义框相交，或垂直间距不超过 28 pt，且水平重叠比例至少 0.35，则把文本块并入语义框；
4. 反复扩展直到不再变化；
5. 水平方向再加页面宽度 0.8% 的 margin，垂直方向加页面高度 1.2%；
6. 裁到页面范围，并重新转换成 `bbox_1000`。

这一步只使用文本块几何边界，不把 PDF 文本当作事实来源，也不做 OCR 降级。

### 5.4 高清渲染

- 默认 200 DPI；
- 每个区域生成 `DrawingCrop`：page、region_id、type、bbox、decision、reason、pixel size 和 PNG bytes；
- 生产接入中 PNG 可只在本次模型请求内存中存在；
- 必须持久化 `drawing_regions` JSONL，其中包含 input ID、源文件 SHA256、页码、bbox、类型和稳定 region ID，以支持事实溯源；
- “完整 PDF 图纸溯源”与裁剪图是否落盘是两个独立功能。完整 PDF 应继续通过报告 runtime 的 `drawing_pdf_path` 提供。

## 6. 第二阶段：高清区域事实提取

### 6.1 输入结构

```json
[
  {"type": "text", "text": "<事实提取 prompt + contract>"},
  {"type": "text", "text": "REGION p1_notes | source page 1 | type notes | coordinates [..]"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
  {"type": "text", "text": "REGION p1_title | source page 1 | type title_block | coordinates [..]"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]
```

### 6.2 提取规则

- 只保存图纸明确写出的事实；
- 不推断材料、工艺、拔模方向、尺寸、表面要求或 2D/3D 对应关系；
- 覆盖标题栏、单位、通用公差、材料/部件关系、明确尺寸与公差、制造约束；
- Notes 的每个编号/字母/连字符子项分别输出为一条 `global_note`；
- 标准号、图号、数字、工程符号应逐字符保真；
- 排除配置矩阵噪声；
- 无法辨认时不猜测；
- 每条事实必须引用本消息提供的一个或多个 region ID。

### 6.3 Observation 输出契约

```json
{
  "observations": [
    {
      "kind": "material",
      "value": "ABS",
      "unit": null,
      "confidence": 0.95,
      "source_region_refs": ["p1_material"],
      "source_text": "MATERIAL: ABS"
    }
  ]
}
```

`kind` 常用值包括 `material`、`general_tolerance`、`part_name`、`document_number`、`revision`、`dimension_units`、`scale`、`manufacturing_constraint`、`global_note`、`dimension`、`tolerance`、`wall_thickness`、`draft_angle`、`radius`、`hole_diameter`、`hole_depth` 等。

## 7. Observation 校验、ID 与溯源

实现位置：`tools/dfm/service.py::_validate_agent_observations`。

每条 proposal 必须：

- 只包含允许字段：`kind`、`value`、`unit`、`confidence`、`source_region_refs`、`source_text`；
- kind 匹配 `^[a-z][a-z0-9_.-]{0,99}$`；
- value 是非空 scalar，不能是 object/list；
- unit 为 null 或不超过 32 字符的非空字符串；
- confidence 是 0–1 的数字，不能是 bool；
- `source_region_refs` 包含 1–20 个唯一 ID，并且全部存在于本次 `drawing_regions`；
- `source_text` 可选，非空且不超过 4000 字符；
- 总数不超过 200 条；
- 重复 observation 被拒绝。

稳定 Observation ID 由以下内容的规范 JSON 做 SHA-256 后截断生成：

```text
input_id + kind + value + unit + source_region_refs
```

持久化 provenance 至少包含：

```json
{
  "provider": "hermes_agent_event_loop",
  "provider_version": "2.0.0",
  "source_type": "DWG",
  "input_sha256": "...",
  "region_refs": ["p1_title_block"],
  "pages": [1],
  "bboxes_1000": [[0, 568, 1000, 1000]],
  "original_text": "..."
}
```

此外 `source_refs` 指向：

```text
artifact:<drawing_regions_artifact_id>#region=<region_id>
```

注意：当前 `ObservationRecord.region_refs` 顶层字段用于 CAD Region/Fusion 语义，图纸裁剪 region ID 保存在 provenance 和 source_refs 中，不应未经契约调整就混入 CAD region_refs。

## 8. 如何复用主 Agent 模型而不污染主上下文

### 8.1 当前运行时快照

Hermes 每个 turn 开始时，`agent/turn_context.py` 调用：

```python
set_runtime_main(
    agent.provider,
    agent.model,
    base_url=agent.base_url,
    api_key=agent.api_key,
    api_mode=agent.api_mode,
)
```

`agent/auxiliary_client.py` 中增加/保留 `get_runtime_main()`，返回进程内快照：

```python
{
    "provider": ...,
    "model": ...,
    "base_url": ...,
    "api_key": ...,
    "api_mode": ...,
}
```

API key 只能在进程内传递：不得写日志、Manifest、Artifact、tool result 或异常 details。

### 8.2 Tool adapter 注入

在 `tools/dfm_tool.py::_call` 中，仅当 `kind == "analysis"` 且 `action == "discover"` 时：

```python
params["_main_runtime"] = get_runtime_main()
```

该参数由 adapter 内部添加，不来自模型工具参数，因此不会出现在模型发起的 tool arguments 中。

### 8.3 内部模型调用

`DFMService._call_main_vision` 使用 `agent.auxiliary_client.call_llm`：

```python
response = call_llm(
    task="vision",
    provider=runtime["provider"],
    model=runtime["model"],
    base_url=runtime.get("base_url") or None,
    api_key=runtime.get("api_key") or None,
    api_mode=runtime.get("api_mode") or None,
    messages=[{"role": "user", "content": content}],
    temperature=0,
    max_tokens=...,
    timeout=480,
)
```

随后使用 `extract_content_or_reasoning(response)` 兼容 reasoning model 的空 content 情况。

重要迁移要求：应保证这里 **严格使用当前主 runtime，失败时直接返回 `drawing_vision_failed`**。如果新版 `call_llm(task="vision")` 会在显式 provider 不可用时自动切换到其他辅助 provider，需要增加 strict/no-fallback 选项或直接走明确客户端解析，不能悄悄使用第二套模型。

### 8.4 为什么不会进入主 Agent 上下文

内部调用直接传入一个新的 messages 数组，只包含本阶段 prompt 与图像，不包含主会话 history、system prompt 或工具 schemas。调用结果在 `DFMService` 内解析并持久化。主 Agent 的消息历史只记录：

```text
assistant -> dfm_analysis(discover)
tool      -> discovery 的结构化结果或错误
```

不会记录 page images、crop images、规划 JSON 或提取 JSON。

## 9. Discovery 状态机

相关状态位位于：

```text
manifest.capabilities.drawing_discovery.inputs[input_id]
```

典型状态：

```json
{
  "status": "completed",
  "page_count": 2,
  "crop_status": "completed",
  "region_count": 6,
  "interpretation_status": "completed",
  "interpretation_provider": "hermes_agent_event_loop",
  "interpretation_version": "2.0.0",
  "observation_count": 41
}
```

`discover` 流程：

1. `_refresh_drawing_visual` 生成/刷新 deterministic drawing diagnostics；
2. `_pending_drawing_interpretations` 找出未完成的 drawing input；
3. 如果存在 `_main_runtime` 且项目同时包含 STEP/Parasolid，使用
   `_discover_geometry_and_drawings_in_parallel` 同时启动 2D 解释分支和 3D 几何发现；
4. 若只有图纸，则逐个执行 `_interpret_drawing_in_background`；
5. 等待相关分支结束，重新加载 Manifest，继续 clarification、fusion 和 snapshot；
6. 正式工具调用不再向主 Agent 返回 `agent_interpretation_required`。

为了兼容单元测试和调试，当前直接调用 `DFMService.analysis("discover")` 且不传 `_main_runtime` 时仍会返回旧的 `agent_interpretation_required` 手动协议。迁移到新代码时可以保留这一兼容路径，也可以在所有生产入口确认切换完成后删除，但不要让正式工具调用回退到可见对话流程。

## 10. 当前生产接入的核心伪代码

```python
def _interpret_drawing_in_background(project_id, input_id, runtime):
    page_context = _drawing_context(project_id, input_id)

    crop_content = replace_first_prompt(
        page_context.content,
        BACKGROUND_CROP_PROMPT + page_context.meta,
    )
    crop_response = call_main_vision(crop_content, runtime, max_tokens=6000)
    regions = decode_json(crop_response)["regions"]
    if not regions:
        raise DFMError("drawing_crop_plan_empty", ...)

    region_context = submit_at_latest_revision(
        lambda revision: _submit_crop_plan(
            project_id, input_id, regions, revision
        )
    )

    observation_content = replace_first_prompt(
        region_context.content,
        BACKGROUND_EXTRACTION_PROMPT + region_context.meta,
    )
    observation_response = call_main_vision(
        observation_content,
        runtime,
        max_tokens=10000,
    )
    observations = decode_json(observation_response)["observations"]

    submit_at_latest_revision(
        lambda revision: _submit_observations(
            project_id, input_id, observations, revision
        )
    )

def _discover_geometry_and_drawings_in_parallel(project_id, input_ids, runtime):
    with ThreadPoolExecutor(max_workers=2) as executor:
        geometry = executor.submit(_persist_geometry_candidates, project_id)
        drawings = executor.submit(interpret_each_drawing, input_ids, runtime)
        drawings.result()
        try:
            geometry.result()
        except ManifestConflict:
            # 只重放持久化；内容寻址的几何产物可以复用。
            _persist_geometry_candidates(project_id)
    return load_latest_manifest(project_id)
```

实验脚本第二次调用使用 12000 tokens；当前生产接入使用 10000。迁移时应根据最大区域数和真实图纸测试决定，建议先保持 12000 以避免长 Notes/BOM 被截断，再基于使用量数据调整。

## 11. 必须保留的并发与一致性控制

- ManifestStore 必须继续使用项目级锁和 `expected_revision` 做原子写入；
- 2D 与 3D 并行期间，图纸提交必须在每次写入前读取最新 revision，并仅对
  `manifest_conflict` 做有限重试；输入 ID、源文件哈希和分析上下文仍必须保持有效；
- 3D 几何提交若因 2D 先写入而冲突，应等待 2D 分支结束后重放持久化；不能覆盖新 Manifest；
- 2D 内部第二轮必须等待第一轮裁剪和区域持久化完成，不能把这两个有依赖的模型请求并行化；
- runtime 在工具调用入口做一次快照，避免长视觉调用期间另一个会话改变进程级 runtime；
- API key 不得进入任何可序列化状态；
- 每个 drawing input 独立执行两次视觉调用，并独立持久化。

## 12. 输入发现配套修复（不要漏掉）

算法能运行的前提是 PDF/图片先被登记。旧版 DFM Skill 描述明确包含文件格式，新版曾缩成泛化描述，导致模型只登记 STEP。

迁移时必须同时保留：

1. `skills/manufacturing/dfm-analysis/SKILL.md` frontmatter description 在 60 字符截断限制内明确写出 `STEP/STP`、`PDF`、`PNG/JPG` 和 `DFM`；
2. Skill 工作流要求每个附件各调用一次 `dfm_project(add_input)`，CAD 和图纸同时存在时两者都必须登记；
3. `DFM_PROJECT_SCHEMA.description` 重复这一硬约束；
4. `agent/context_references.py` 对 PDF/PNG/JPG/JPEG 附件注入 DFM 专用提示：先为每个附件 add_input，再 status/discover/plan；
5. 测试同时覆盖 STEP + PDF 附件，不能只测单文件。

当前简短且不会被 60 字符截断的描述是：

```yaml
description: Analyze STEP/STP CAD and PDF/PNG/JPG drawings for DFM.
```

## 13. 文件级迁移清单

拉取新代码后按以下顺序 port，而不是直接覆盖整个文件。

### A. 算法内核

- `tools/dfm/drawing_pipeline/core_pipeline.py`
  - `render_overviews`
  - `_validate_regions`
  - `_deduplicate_document_regions`
  - `_snap_to_text`
  - `render_crops`
  - 空区域拒绝
- `tools/dfm/drawing_pipeline/interface.py`
  - `DrawingImage`
  - `DrawingCrop`
  - `render_overviews`
  - `prepare_crops`
- `tools/dfm/analyzers/drawing.py`
  - 只做确定性文档处理；不得在 analyzer 内配置模型客户端
  - 暴露 `render_overviews` 和 `prepare_crops`

### B. DFM 服务编排

- `tools/dfm/service.py`
  - 两个后台 JSON prompt
  - `_decode_background_json`
  - `_call_main_vision`
  - `_interpret_drawing_in_background`
  - `_drawing_context`
  - `_submit_crop_plan`
  - `_validate_agent_observations`
  - `_submit_observations`
  - `discover` 自动执行 pending drawing interpretations
  - revision、Artifact、Manifest state 和 provenance

### C. 主 Agent runtime 复用

- `agent/turn_context.py`
  - 每 turn 调用 `set_runtime_main`
- `agent/auxiliary_client.py`
  - 保留 runtime provider/model/base URL/key/mode
  - 提供进程内 `get_runtime_main()` 快照
  - 检查新版 vision routing 是否会自动 fallback
- `tools/dfm_tool.py`
  - `discover` 时内部注入 `_main_runtime`
  - schema 明确禁止 browser/vision_analyze/manual drawing_context 正常流程

### D. Skill 与附件入口

- `skills/manufacturing/dfm-analysis/SKILL.md`
- `agent/context_references.py`
- `tools/dfm_tool.py::DFM_PROJECT_SCHEMA`

### E. 测试

- `tests/tools/dfm/test_drawing_pipeline.py`
- `tests/tools/dfm/test_skill_contract.py`
- `tests/agent/test_context_references.py`

### F. 完整 PDF 报告溯源（与裁剪算法独立，但当前分支已发现回归）

- `tools/dfm/reporting/html/runtime_adapter.py` 必须继续提供 `resources.drawing_pdf_path`；
- `tools/dfm/reporting/html/template.py` 将完整 PDF base64 嵌入报告并由 `openEmbeddedPdf()` 打开；
- 可编辑报告会清除任意 `onclick`，这是安全边界，不应全局关闭；
- 演示模式的可信原报告 iframe 需要 `sandbox="allow-scripts allow-popups"`，否则浏览器会阻止 `window.open(blobUrl)`；
- 不要增加 `allow-same-origin`；
- 旧的自包含 `report.html` 不会因代码更新自动变化，必须重新生成报告。

## 14. 推荐的迁移步骤

1. 拉取新代码后先比较上述文件，不要直接 cherry-pick 大块旧 core；
2. 确认新版 DFM contracts、tool registry 和 `auxiliary_client.call_llm` 签名；
3. 先迁移 deterministic drawing pipeline 并跑纯单元测试；
4. 迁移 runtime snapshot，但确保密钥不序列化；
5. 将两次 side-call 接到 `discover`；
6. 迁移 input discovery/Skill 描述，确保 PDF 确实 add_input；
7. 用 fake vision callable 跑完整两阶段测试；
8. 用真实多页 PDF + 当前主模型做一次 E2E；
9. 检查项目 Manifest、drawing regions、observations、report runtime 和完整 PDF 按钮；
10. 最后再考虑删除旧的手动 `drawing_context` 工具路径。

## 15. 最小测试矩阵

### 15.1 纯算法

- PDF 全页渲染成功，页码/尺寸正确；
- PNG/JPG 单页渲染成功；
- 合法 bbox 被 padding 并吸附到文本块；
- 越界页码、反向 bbox、过小区域、非法 decision 被拒绝；
- 空 regions 被拒绝；
- 重复 region ID 被消歧；
- 重复标题栏按规则去重；
- 高清裁剪在内存生成且没有意外临时文件。

### 15.2 服务契约

- 第一次 fake vision 返回 regions，第二次返回 observations；
- `discover` 恰好调用 vision 两次；
- 两次收到同一个 `_main_runtime.model/provider`；
- 完成后 `interpretation_status == "completed"`；
- `region_count > 0`、`observation_count` 正确；
- observation 引用未知 region 时拒绝；
- revision 冲突时拒绝；
- 模型返回 fenced JSON、reasoning-only content 时可解析；
- 模型返回无 JSON、空 regions、缺 observations 字段时明确失败；
- API key 不出现在 tool result、日志和 Manifest。

### 15.3 Agent 入口

- 同一用户消息附带 STEP + PDF 时，两者都 add_input；
- Manifest `input_mode == "fusion"`；
- 正式 `dfm_analysis(discover)` 不返回 `agent_interpretation_required`；
- 对话轨迹中不出现 browser、`vision_analyze`、`drawing_context`、`submit_crop_plan`、`submit_observations`；
- 主 Agent 上下文不含 base64 页面/裁剪图或两个视觉模型的原始回复。

### 15.4 真实 E2E 验收

- 多页工程图中 Notes 子项无静默遗漏；
- 标题栏身份、图号、版本、单位、比例和通用公差可提取；
- 材料与部件关系不被拆散；
- 配置矩阵噪声不进入 observations；
- 每条 observation 可追到输入 SHA256、页码、bbox、region ID 和 source text；
- 完整原始 PDF 可从最终报告打开；
- STEP + drawing fusion 不把图纸语义当成已确认几何事实。

## 16. 当前分支已执行的验证

本次实现后执行过：

```text
tests/tools/dfm/test_drawing_pipeline.py
tests/tools/dfm/test_skill_contract.py
```

结果：`11 passed, 1 skipped`。

另外针对后台双调用及旧手动流程的定向测试：`2 passed`；附件/Skill 相关定向测试：`4 passed`。

完整 PDF 演示 iframe 的 sandbox 修复有独立测试通过。完整 HTML 编辑器 E2E 依赖 Playwright Chromium；迁移到新代码后应在具备该浏览器依赖的环境再次执行完整报告测试。

## 17. 已知风险与迁移时建议改进

1. **严格主模型复用**：确认辅助 vision router 不会在主模型不可用时回退其他 provider；需求是 fail closed，而不是悄悄换 API。
2. **provider 命名**：现有 provenance 仍使用兼容名称 `hermes_agent_event_loop`，但视觉已在工具内部执行。新版本可增加新的 provider version 或迁移字段，但需考虑旧 Manifest 兼容。
3. **同步等待**：当前“后台”不会污染对话，2D 与 3D 已并行重叠，但 `discover` 仍会等待两分支汇合。若改成真正异步 Job，必须保留 runtime snapshot、幂等和 revision 检查。
4. **超时与重试**：两次请求各 480 秒。网络重试不能跨 revision，也不能在第一次成功、第二次失败后把状态误标为 completed。
5. **空 observations**：完整区域确实没有支持事实时可允许空 observations；空 crop regions 永远不允许。
6. **多图纸项目**：每个 drawing input 独立两次调用；不要把不同源文件的 region ID 混在同一次 observation 校验中。
7. **大图纸上下文**：50 页或 48 个区域可能超过部分模型限制。未来可在保持跨页覆盖的前提下做可审计分批，不能无提示截断。
8. **完整 PDF 与裁剪证据区分**：完整 PDF 是用户查看原图的溯源入口；region/bbox 是事实级机器溯源。不要把“完整 PDF 溯源”误改成只展示裁剪图片。

## 18. 一句话验收标准

用户同时提交 STEP 和 PDF 后，只需看到一个 DFM Discovery 工具过程；DFM 在内部并行执行
3D 几何发现与完整 2D 视觉分支，并用当前主 Agent 的同一模型和凭据顺序完成“全页规划 +
高清区域提取”。主对话只收到两分支汇合后经过校验和持久化的结果，最终报告仍能打开完整
原始 PDF，并且每条 2D 事实都能追到源文件、页码和区域。
