# DFM 2D 语义裁剪与多模态集成溯源

## 1. 文档目的

本文记录 2026-09-21 将 `dfm-2D-crop` 的二维图纸算法移植到最新
`dfm-hermes-agent` 分支时所做的代码改动、调用关系、数据来源、持久化边界和验证结果。

它回答以下问题：

- 原 OCR 被哪些代码替换；
- PDF 是在哪里进入项目、在哪里被渲染、由谁进行视觉理解；
- 主 Agent、后台视觉调用和最终报告上下文之间如何分工；
- 完整二维图纸如何溯源；
- 合并时保留了哪些主分支能力；
- 后续如何审查或再次移植这组改动。

更详细的算法说明和再次移植步骤见
[`dfm-2d-semantic-crop-multimodal-porting-guide.md`](dfm-2d-semantic-crop-multimodal-porting-guide.md)。

## 2. 代码来源与合并基线

| 项目 | 值 |
|---|---|
| 目标分支 | `dfm-hermes-agent` |
| 拉取后的目标基线 | `93cd2c20f2298528f95c9c412268398d577a5378` |
| 目标基线主题 | Desktop 不再显示 preview、HTML 报告异步生成与性能优化 |
| 算法来源分支 | `dfm-2D-crop` |
| 算法来源提交 | `f95519bcca5824a8300e0cf542d7ac0e6fb2ee55` |
| 原实验参考 | `D:\hermes_deployment\hermes-agent-dfm-hermes-agent2\experiments\dfm_visual_extraction\scripts\semantic_crop_pipeline.py` |
| 临时 stash | `4488279c030eae7da9c8895aea92b13b9a531535`，完成恢复与验证后已删除 |

本次没有创建提交；集成代码当前处于 Git 暂存区，便于提交前统一审查。

### 2.1 改动文件数量（截至 2026-09-21）

本次暂存区合计改动 **19 个文件**，其中：

- **14 个代码文件**：13 个 Python 实现/自动化测试和 1 个编辑器提取器 JavaScript；
- **5 个行为说明、资源清单及文档文件**：1 个运行时 Manifest、1 个 Skill、
  1 个管线 README、2 个集成文档。

14 个代码文件如下：

1. `agent/auxiliary_client.py`
2. `agent/context_references.py`
3. `tests/agent/test_context_references.py`
4. `tests/tools/dfm/test_drawing_pipeline.py`
5. `tests/tools/dfm/test_reporting_html.py`
6. `tests/tools/dfm/test_skill_contract.py`
7. `tests/tools/dfm/test_tool_surface.py`
8. `tools/dfm/analyzers/drawing.py`
9. `tools/dfm/drawing_pipeline/core_pipeline.py`
10. `tools/dfm/drawing_pipeline/interface.py`
11. `tools/dfm/reporting/html/editor.py`
12. `tools/dfm/service.py`
13. `tools/dfm_tool.py`
14. `tools/dfm/reporting/html/editor_runtime/extract.js`

5 个行为说明、资源清单及文档文件如下：

1. `tools/dfm/reporting/html/editor_runtime/manifest.json`
2. `skills/manufacturing/dfm-analysis/SKILL.md`
3. `tools/dfm/drawing_pipeline/README.md`
4. `docs/2026-09-21-dfm-2d-semantic-crop-integration-trace.md`
5. `docs/dfm-2d-semantic-crop-multimodal-porting-guide.md`

## 3. 最终运行链路

```text
用户附件 STEP/STP + PDF/PNG/JPG
  │
  ├─ context reference 明确提示每个附件都必须 add_input
  │
  ├─ dfm_project(action=add_input)
  │    └─ 原始完整图纸复制/登记到 DFM 项目 inputs，保留文件哈希和输入 ID
  │
  └─ dfm_analysis(action=discover)
       ├─ 并行分支 A：2D 图纸
       │    ├─ 72 DPI 渲染全部图纸页面，仅在内存中传递
       │    ├─ 当前主 Agent 模型后台调用 1：输出语义裁剪计划 JSON
       │    ├─ 程序校验、补边、去重、文本块吸附
       │    ├─ 200 DPI 渲染高清裁剪，仅在内存中传递
       │    ├─ 当前主 Agent 模型后台调用 2：输出图纸事实 JSON
       │    └─ 校验并持久化 drawing_regions / drawing_observations
       ├─ 并行分支 B：3D STEP/Parasolid 几何发现
       └─ 两分支汇合后继续 clarification、2D/3D 融合、分析与报告
```

这里的“后台”是同一次 `discover` 工具调用内部的同步辅助模型调用。主对话不会收到页面图片、
裁剪图片或中间提示词，只收到最终 DFM 工具结果。视觉调用复用当前主 Agent 的 provider、model、
base URL、API key 和 API mode；没有第二套视觉 API 配置。

## 4. 主要代码改动

### 4.1 主 Agent 运行时复用

文件：`agent/auxiliary_client.py`

- 新增 `get_runtime_main()`。
- 返回当前主 Agent 的运行时快照：provider、model、base URL、API key 和 API mode。
- 快照只在进程内传给 DFM `discover`，不写入 Manifest、Artifact、日志或模型可见参数。
- 现有 `agent/turn_context.py` 继续负责调用 `set_runtime_main()`，因此没有新增用户配置项。

### 4.2 PDF/图片附件进入 DFM

文件：`agent/context_references.py`

- 新增 `_DFM_DRAWING_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}`。
- PDF/PNG/JPG/JPEG 不再按普通二进制附件给出模糊提示。
- `_drawing_reference_block()` 明确要求调用
  `dfm_project(action=add_input)`，并要求 STEP/STP 与所有图纸附件逐个登记。
- STEP 的 opaque 提示也同步强调不得只登记 CAD 而遗漏图纸。

文件：`tools/dfm_tool.py`

- `DFM_PROJECT_SCHEMA` 明确列出 STEP/STP、x_t、PDF、PNG、JPG、JPEG。
- 工具描述要求在 `status`、`discover`、`plan` 前完成全部输入登记。

### 4.3 删除 OCR，改为确定性渲染和语义裁剪

文件：`tools/dfm/drawing_pipeline/core_pipeline.py`

- 移除 RapidOCR 和 OCR fragment 生成。
- `process_file()` 只读取图纸页面元数据。
- `render_overviews()` 以 72 DPI 渲染全部页面。
- `_validate_regions()` 校验页码、区域数量、0–1000 归一化坐标、最小尺寸、区域类型和决策。
- 裁剪区域自动增加 24/1000 padding。
- `_snap_to_text()` 使用 PDF 文本块进行边界吸附，避免截断 Notes、表格行和尺寸标注。
- `_deduplicate_document_regions()` 只保留一份最早标题栏区域。
- `render_crops()` 以 200 DPI 渲染通过校验的高清区域。
- 页面图和裁剪图均以 PNG bytes 在内存中传递，不作为项目 Artifact 落盘。

文件：`tools/dfm/drawing_pipeline/interface.py`

- 管线版本升级为 `3.0.0`。
- `DrawingFragment` 替换为 `DrawingPage`、`DrawingImage` 和 `DrawingCrop`。
- Provider 改为 `hermes_semantic_crop_pipeline`。
- 增加 `render_overviews()` 与 `prepare_crops()` 稳定接口。

文件：`tools/dfm/analyzers/drawing.py`

- 删除原始 OCR 文本和 OCR fragments Artifact。
- Discovery 只落盘 `drawing_diagnostics`。
- 诊断明确包含 `ocr_used=false` 和
  `extraction_mode=model_planned_semantic_crops`。
- 增加页面预览和高清裁剪适配方法。

### 4.4 两轮后台多模态理解

文件：`tools/dfm/service.py`

- 新增 `_BACKGROUND_CROP_PROMPT`：要求模型仅返回严格 JSON 裁剪计划。
- 新增 `_BACKGROUND_EXTRACTION_PROMPT`：要求模型仅返回严格 JSON 图纸事实。
- `_call_main_vision()` 使用 `agent.auxiliary_client.call_llm(task="vision")`，运行时来自主 Agent 快照。
- `_interpret_drawing_in_background()` 在一次 `discover` 内串联两轮视觉调用。
- 当项目同时存在待解释图纸和 STEP/Parasolid 时，
  `_discover_geometry_and_drawings_in_parallel()` 用两个 worker 并行执行完整 2D 分支和 3D 几何发现；
  两轮 2D 调用本身仍保持先规划、后提取的依赖顺序。
- 两个分支在 Fusion 前汇合。Discovery Plan 也将普通几何与 Agent 图纸解释设为并列前置项，
  `discovery.fusion` 同时依赖二者。
- 两分支可能同时更新 Manifest：2D 提交在写入前刷新 revision，并对并发冲突做有限重试；
  若 3D 提交输掉乐观锁竞争，则在 2D 完成后重放持久化，已完成的内容寻址几何产物可复用。
- 空裁剪计划返回 `drawing_crop_plan_empty`，不会静默跳过图纸。
- 无主 Agent runtime 时保留显式 `agent_interpretation_required` 协议，供测试和调试使用。

服务层的数据处理职责如下：

| 方法 | 职责 |
|---|---|
| `_refresh_drawing_visual()` | 初始化图纸元数据与待解释状态 |
| `_drawing_context()` | 组装全部低清页面的多模态消息 |
| `_submit_crop_plan()` | 校验裁剪计划、生成高清裁剪并保存 region 元数据 |
| `_validate_agent_observations()` | 校验事实字段和 `source_region_refs` |
| `_submit_observations()` | 保存候选 Observation 并应用图纸来源策略 |
| `_interpret_drawing_in_background()` | 将上述步骤自动串联到 `discover` |
| `_discover_geometry_and_drawings_in_parallel()` | 并行编排 2D 视觉与 3D 几何发现，并在融合前汇合 |

### 4.5 DFM 工具协议

文件：`tools/dfm_tool.py`

- `discover` 调用前注入 `_main_runtime`，该字段不在模型可见 Schema 中。
- Schema 增加 `submit_crop_plan` 和 `crop_regions`，作为测试/调试协议保留。
- Observation 证据字段由 `source_fragment_refs` 改为 `source_region_refs`。
- 新增可选 `source_text`，用于保留模型识别出的原始图纸文本。
- 正常工作流明确禁止浏览器打开 PDF、禁止调用通用 `vision_analyze`、禁止手工驱动中间动作。
- 保留目标分支对 `start` 和 `render_html` 的后台进度回调。

### 4.6 Skill 入口与行为约束

文件：`skills/manufacturing/dfm-analysis/SKILL.md`

- frontmatter 描述改为：
  `Analyze STEP/STP CAD and PDF/PNG/JPG drawings for DFM.`
- 明确所有附件必须逐个 `add_input`。
- 明确两轮视觉分析在 `discover` 内后台完成并复用主 Agent。
- 明确混合 CAD + 图纸项目中，2D 视觉分支与 3D 几何发现并行运行。
- 明确不得打开浏览器查看图纸，不得配置第二模型端点。
- 报告阶段只使用已经持久化的 drawing observations，不重新解释图纸。
- 保留目标分支的异步 `render_html`、完成通知和禁止快速轮询约束。

### 4.7 完整二维图纸溯源与演示打开

完整 PDF 与裁剪图的作用不同：

- 完整 PDF 是正式输入源，保留在项目 inputs 中，具有 `input_id`、SHA-256 和源文件路径；
- 裁剪图只是模型理解所需的瞬时内存数据，不承担最终文档溯源；
- `drawing_regions` 保存源 `input_id`、输入哈希、页码和 `bbox_1000`；
- `drawing_observations` 通过 `source_region_refs` 指回区域，并在 provenance 中保存页码和 bbox；
- 报告 Runtime 使用持久化 Observation；完整图纸入口仍指向原始 PDF，而不是裁剪图片。

文件：`tools/dfm/reporting/html/editor.py`、
`tools/dfm/reporting/html/editor_runtime/extract.js` 和
`tools/dfm/reporting/html/editor_runtime/manifest.json`

- 把 live report iframe 的 sandbox 从 `allow-scripts` 扩展为
  `allow-scripts allow-popups allow-popups-to-escape-sandbox`，使“完整二维图纸”
  的 PDF Blob 新窗口不会继续继承演示 iframe 的沙箱限制。
- 没有加入 `allow-same-origin`。
- 撤销 `startInPresentation` 自动演示方案；新生成的报告仍正常打开编辑页面，
  不会自动进入报告、放大或请求浏览器全屏。
- `extract.js` 只为原报告中已有点击行为保存随机 action key、DOM 路径和
  `drawing-pdf`/`live` 类型；实际 `onclick` 仍会从编辑画布副本中删除。
- 用户在编辑页点击溯源或证据按钮时，外层可信桥接器依据按钮几何命中 action key，
  以 `present(false, false)` 打开当前页的非全屏 live report，再通过每个 iframe 的
  随机 token 转发一次 `activate` 消息；iframe 只点击对应的原始报告节点。
- “2D 图纸”在用户点击时先同步打开空白窗口，再从已校验的内嵌原报告中解出 PDF
  Blob 并导航该窗口，避免异步解压导致浏览器丢失用户激活而拦截弹窗。
- 没有恢复任意脚本执行，没有把 handler 字符串跨边界执行，也没有降低原沙箱隔离。

## 5. 持久化数据与上下文归属

| 数据 | 生成者 | 是否进入主聊天上下文 | 是否持久化 |
|---|---|---:|---:|
| 原始完整 PDF/PNG/JPG | `dfm_project.add_input` | 仅路径/登记信息 | 是，作为项目输入 |
| 72 DPI 整页图片 | 图纸管线 | 否 | 否 |
| 裁剪计划 JSON | 主 Agent 模型后台调用 1 | 否 | 区域元数据持久化 |
| 200 DPI 高清裁剪 | 图纸管线 | 否 | 否 |
| Observation JSON | 主 Agent 模型后台调用 2 | 否 | 是 |
| `discover` 最终结果 | DFM Service | 是 | 相关 Manifest 数据持久化 |
| 报告 `llm_content` | 当前主 Agent | 是 | 是，随后异步生成 HTML |

因此，二维视觉理解没有绕过模型：模型参与裁剪规划和事实提取；但图片与中间消息属于
DFM 工具内部辅助调用，不污染主对话历史。主 Agent 的普通对话上下文只接收最终工具结果。

## 6. 合并时明确保留的目标分支功能

- `render_html` 继续通过 JobManager 异步排队。
- `render_html` 立即返回 `accepted=true`，完成后发送进度/完成通知。
- `result` 只有在 Run 达到 `succeeded` 且 `report.html` 已附加后才可调用。
- `STAGE_REPORT_QUEUED`、`STAGE_REPORT_RENDERING`、`STAGE_REPORT_LAYOUT`、
  `STAGE_REPORT_PACKAGING` 等阶段未被旧分支覆盖。
- Desktop 不显示旧 preview 的目标分支行为未回退。

## 7. 测试与验证溯源

相关测试文件：

- `tests/tools/dfm/test_drawing_pipeline.py`
- `tests/tools/dfm/test_tool_surface.py`
- `tests/tools/dfm/test_skill_contract.py`
- `tests/tools/dfm/test_reporting_html.py`
- `tests/agent/test_context_references.py`

本次验证结果：

- DFM 测试集（排除需要本机 Chromium 的 HTML 实际渲染文件）：
  `297 passed, 13 skipped`。
- 主 Agent runtime、turn context、图纸管线和工具表面联合回归：
  `36 passed, 1 skipped`。
- 最终专项回归：`20 passed, 1 skipped`。
- 并行编排加入后的图纸管线与 DFM Service 联合回归：`29 passed, 1 skipped`。
- Ruff 静态检查：`All checks passed`。
- HTML 溯源修复的两个专项测试：`2 passed`。
- 使用系统 Python 和真实 Playwright Chromium 重新生成完整报告后，从默认编辑页
  真实点击第二页“完整溯源”：非全屏 live report 正常打开且溯源抽屉已经展开。
- 同一端到端验证确认 `document.fullscreenElement === null`、页面无脚本错误，且
  从编辑页真实点击“2D 图纸”能够创建 PDF 弹窗。
- HTML 全套中的两个实际渲染测试因本机缺少 Playwright Chromium 未执行成功；
  该结果来自项目 `.venv`；失败发生在浏览器布局提取启动阶段，不是语义裁剪或
  异步报告逻辑断言失败。使用装有 Chromium 的系统 Python 已完成上述真实验证。

## 8. 审查与提交建议

当前集成改动已经暂存，可用以下命令审查：

```powershell
git status --short
git diff --cached --stat
git diff --cached -- tools/dfm/service.py
git diff --cached -- tools/dfm/drawing_pipeline/core_pipeline.py
git diff --cached -- tools/dfm_tool.py
```

`docs/dfm-harness-engineering-vs-hermes.md` 是原有未跟踪文件，本次没有修改或暂存。

建议提交信息：

```text
feat(dfm): replace drawing OCR with main-agent semantic crop vision
```
