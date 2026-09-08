# DFM HTML 最终报告替换改动追溯

记录日期：2026-09-07

架构纠偏日期：2026-09-08。纠偏内容是将第一次 Hermes OCR 解释生成的 `drawing_observations` 恢复为报告阶段唯一的图纸语义来源，删除报告阶段二次读取 OCR 的路径。

报告编辑纠偏日期：2026-09-08。最后一次 Hermes LLM 被明确为报告编辑器：基于持久化 observations 和确定性 Runtime 进行用户语言本地化、综合总结、风险解释和行动建议，不得机械复制 Runtime 英文。HTML 模板中的英文界面微标题同步改为中文。

成功条件纠偏日期：2026-09-08。HTML-capable run 在确定性 Runtime 完成后进入 `reporting/report_editing/98%`，不再提前标记 `succeeded`。新增既有 `dfm_analysis` 工具内的 `report_context` action，将完整 Runtime 内联交给当前 Hermes Agent；只有 `render_html` 成功生成、校验并登记 `report.html` 后，run 才进入 `succeeded/complete/100%`。

Desktop 自动展示增量日期：2026-09-08。Desktop 现在监听既有 `dfm_analysis` 工具完成事件；当且仅当 run 成功且返回的 artifact 为 `report_html` 时，将 `report.html` 路径交给 Desktop 已有的 HTML 预览面板自动展示。该增量没有修改 DFM 后端、报告生成器或成功条件。

本次纠偏及报告编辑更新涉及 **11 个既有集成范围内文件**，没有增加新的生产模块或改变下文集成总文件数：

1. `tools/dfm/reporting/html/runtime_adapter.py`
2. `tools/dfm/runtime/jobs.py`
3. `tools/dfm/service.py`
4. `tools/dfm_tool.py`
5. `skills/manufacturing/dfm-analysis/SKILL.md`
6. `tests/tools/dfm/test_reporting_html.py`
7. `tests/tools/dfm/test_drawing_pipeline.py`
8. `tests/tools/dfm/test_skill_contract.py`
9. `html.md`
10. `DFM_HTML_INTEGRATION_CHANGELOG.md`
11. `tools/dfm/reporting/html/template.py`

成功条件纠偏增量涉及 **12 个文件**：`tools/dfm/contracts.py`、`tools/dfm/runtime/jobs.py`、`tools/dfm/service.py`、`tools/dfm_tool.py`、DFM Skill、5 个对应测试文件，以及 `html.md` 和本追溯文档。没有修改 OCR、OCCT、Evaluation、Evidence 或 HTML renderer 内部实现。

## 结论

截至成功条件纠偏，DFM HTML 主 pipeline 包含 **24 个实现/配置/测试/使用说明文件**；加上本追溯文档，共 **25 个文件**。随后 Desktop 自动展示增量修改 **6 个既有文件**，因此当前 `dfm-html-report` 分支相对任务起点共涉及 **31 个文件**。

这里的文件数包含 HTML 模板、两份离线 JavaScript 依赖、测试、打包配置和说明文档。HTML 集成及后续纠偏涉及的既有 Python 文件有 **5 个**：

1. `tools/dfm/reporting/result_assembler.py`
2. `tools/dfm/contracts.py`
3. `tools/dfm/runtime/jobs.py`
4. `tools/dfm/service.py`
5. `tools/dfm_tool.py`

`render_html` action 是当前 Hermes Agent 向 HTML generator 交付 LLM 总结的报告阶段接口。Runtime adapter 会把 Discovery 阶段第一次 Hermes 已持久化并验证的 `drawing_observations` JSONL 原样映射进 `runtime_data.jsonl`，再与确定性 2D/3D artifacts 汇合；它不会重新解释 OCR，也不会代替 Agent 生成 `llm_content.jsonl`。

## 一、`tools/dfm/reporting/` 内部更改

这一组是 HTML 报告实现本身，共 **7 个文件**。除已有的报告组装器外，其余全部封装在新增的 `html/` 子包中。

| 文件 | 状态 | 职责 |
|---|---|---|
| `tools/dfm/reporting/result_assembler.py` | 修改 | 保留 JSON/Markdown，移除原 PPT renderer 调用。 |
| `tools/dfm/reporting/html/__init__.py` | 新增 | HTML reporting 子包公开入口。 |
| `tools/dfm/reporting/html/generator.py` | 新增 | 对既有 HTML generator 做内部调用包装和错误处理。 |
| `tools/dfm/reporting/html/runtime_adapter.py` | 新增 | 将已有 plan、inputs、`drawing_observations`、report 和 2D/3D artifacts 映射成 `runtime_data.jsonl`，不做语义或工程重算。 |
| `tools/dfm/reporting/html/template.py` | 新增 | 纳入既有 HTML 模板，并将报告内固定的英文微标题本地化为中文。 |
| `tools/dfm/reporting/html/vendor/OrbitControls.r128.js` | 新增 | HTML 离线 3D 依赖。 |
| `tools/dfm/reporting/html/vendor/three.r128.min.js` | 新增 | HTML 离线 3D 依赖。 |

reporting 内部的依赖方向为：

```text
runtime/jobs.py -> html/runtime_adapter.py -> runtime_data.jsonl
service.py      -> html/generator.py -> html/template.py -> html/vendor/*.js
```

`html/` 不反向依赖 job、service 或 model tool。外层分别调用 runtime adapter 和 generator，HTML 实现仍可在 reporting 边界内独立替换。

它对 reporting 外部只有数据契约依赖：复用 `ArtifactRecord`、`InputRecord`、`PlanRecord` 和 `DFMError`。这些是 DFM 现有公共 contracts/errors，不包含分析器、service 或任务调度行为。

## 二、reporting 外部的运行时更改

跨出 reporting 文件夹的运行时代码有 **4 个文件**。主体仍是最终报告接线；其中 `service.py` 额外向原有 `drawing_context` 返回页清单，保证 Discovery 阶段第一次语义整理完整：

| 文件 | 改动 | 必要性 |
|---|---|---|
| `tools/dfm/contracts.py` | 增加 `reporting` Run 状态和 `report_editing` stage。 | 将“确定性计算完成”和“完整报告成功”区分为不同状态。 |
| `tools/dfm/runtime/jobs.py` | JSON/Markdown 和 evidence 完成后，将本次 plan 固定的 Observation IDs 与语义 artifact 交给 runtime adapter；存在 HTML Runtime 时保持 `reporting/98%`，HTML 登记后才转为成功。 | 防止 run 在最终报告缺失时提前显示成功。 |
| `tools/dfm/service.py` | 增加 `report_context` 与 `render_html` 报告动作；`drawing_context` 返回 `available_pages`，指定页时返回该页完整 fragments。 | 确保第一次 Hermes 覆盖全部页面，并让报告阶段无需 terminal/read-file 即可获得完整 Runtime、调用 generator 并登记 HTML。 |
| `tools/dfm_tool.py` | 扩展既有 `dfm_analysis` schema，暴露 `report_context`/`wait_seconds`，并明确 HTML 是成功门槛。 | 让当前 Agent 能在同一工具边界内等待 Runtime、提交 `dfm-html-llm/v1`，没有新增 model tool。 |

调用关系为单向依赖：

```text
worker/jobs.py -> reporting/html/runtime_adapter.py -> runtime_data.jsonl
current Hermes Agent -> dfm_analysis(report_context -> render_html)
  -> service.py -> reporting/html/generator.py -> report.html
```

`jobs.py` 不生成报告文案；`service.py` 不总结 OCR；`dfm_tool.py` 只声明交付契约。三者不会侵入 OCR 提取、几何或 Evaluation。`service.py` 对 `drawing_context` 的小幅扩展只用于保证 Discovery 阶段单次语义整理覆盖全部页面。

以下运行时边界**没有修改**：

- OCR 提取器与 `tools/dfm/drawing_pipeline/` 实现
- OCCT、STEP、Parasolid analyzer
- Evaluation、Evidence 和评分逻辑
- manifest schema 和 artifact 查询接口（Run 状态契约新增 `reporting`）

## 三、reporting 外部的非运行时更改

这些文件不会扩大 DFM 运行时模块之间的耦合。

### 打包声明：2 个文件

| 文件 | 用途 |
|---|---|
| `MANIFEST.in` | 确保源码包包含 HTML vendor 资源。 |
| `pyproject.toml` | 确保 wheel 包含 HTML 子包和 vendor 资源。 |

### 测试：9 个文件

| 文件 | 用途 |
|---|---|
| `tests/tools/dfm/test_contracts.py` | 验证 `running -> reporting -> succeeded` 状态转换契约。 |
| `tests/tools/dfm/test_jobs.py` | 验证 HTML Runtime 只进入 `reporting/98%`，HTML artifact 登记后才成功。 |
| `tests/tools/dfm/test_reporting_html.py` | 新增 HTML adapter、drawing semantics 接线、renderer 和离线资源专项测试；使用临时最小契约夹具，不依赖未跟踪的 `DFM-HTML/example`。 |
| `tests/tools/dfm/test_drawing_pipeline.py` | 验证 drawing context 暴露全部页码并支持按页读取。 |
| `tests/tools/dfm/test_result_assembler.py` | 验证 JSON/Markdown 继续生成且不再走 PPT。 |
| `tests/tools/dfm/test_m1_e2e.py` | 更新最终产物预期，不再要求 PPT。 |
| `tests/tools/dfm/test_m25_e2e.py` | 更新最终产物预期，不再要求 PPT。 |
| `tests/tools/dfm/test_skill_contract.py` | 验证 skill 将 HTML 作为主要可读报告。 |
| `tests/tools/dfm/test_tool_surface.py` | 验证既有 `dfm_analysis` 暴露 `render_html` 和 `llm_content`。 |

### 文档：3 个文件

| 文件 | 用途 |
|---|---|
| `skills/manufacturing/dfm-analysis/SKILL.md` | 要求当前 Agent 仅在 Discovery 阶段完整读取 OCR 并持久化 observations；报告阶段只读结构化语义与 Runtime。 |
| `html.md` | 任务实施和验收说明。 |
| `DFM_HTML_INTEGRATION_CHANGELOG.md` | 本追溯文档。 |

## 四、数量汇总

| 分组 | 文件数 | 是否形成运行时耦合 |
|---|---:|---|
| reporting 内部 | 7 | 仅 reporting 包内部 |
| reporting 外部报告阶段接线 | 4 | 是，但仅限状态契约、runtime 生成和 Agent 内容交付 |
| 打包声明 | 2 | 否 |
| 测试 | 9 | 否 |
| 文档 | 3 | 否 |
| Desktop HTML 自动展示 | 6 | 仅 Desktop 事件路由、既有预览状态及其回归测试，不依赖 DFM 内部模块 |
| **总计** | **31** | OCR/几何/评估层无改动 |

另一个统计口径：相对任务开始时，既有已跟踪文件有 20 个实际内容差异；新增实现/测试/资源文件 7 个；任务说明文档和本追溯文档 2 个。

解耦性结论：HTML renderer 的实现和资源全部位于 reporting 内；外部接线用于完成“worker 产 Runtime、当前 Agent 产文案、service 登记报告”的两阶段交付。它扩展了既有 DFM tool/service 的报告接口，但没有向分析计算层扩散。

## 五、实际调用链

```text
原 OCR / OCCT / Evaluation / Evidence
  -> Discovery 阶段当前 Hermes Agent 一次性生成 drawing_observations JSONL
  -> result_assembler 继续生成 dfm_report.json、dfm_report.md
  -> runtime adapter 合并 drawing_observations 与 2D/3D artifacts，生成 runtime_data.jsonl
  -> run 进入 reporting/report_editing/98%（尚未成功）
  -> 当前 Hermes Agent 通过 dfm_analysis(report_context) 获取内联 observations + Runtime
  -> Agent 生成 llm_content 并调用 dfm_analysis(render_html)
  -> service 调用既有 HTML generator
  -> llm_content.jsonl、report.html 登记到同一个 run
  -> run 进入 succeeded/complete/100%
```

没有新增 OCR、几何、规则、证据或评分计算，也没有第二个模型客户端。OCR 原文只在 Discovery 阶段由当前 Hermes 会话模型解释一次；报告文案由同一会话模型基于已持久化的 observations 和 Runtime 生成。

## 六、Desktop HTML 自动展示后续增量

| 文件 | 状态 | 职责 |
|---|---|---|
| `apps/desktop/src/lib/dfm-viewer-events.ts` | 修改 | 从成功的 `dfm_analysis` tool-complete 结果中严格提取 `report_html` 路径；忽略失败、未完成和其他 HTML 结果。 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts` | 修改 | 当前会话成功生成报告时调用既有预览状态，自动打开 HTML 右侧预览；后台会话只登记目标，不抢占当前界面。 |
| `apps/desktop/src/lib/dfm-viewer-events.test.ts` | 修改 | 覆盖成功路径、失败 run、非 HTML artifact、空路径及无关工具事件。 |
| `apps/desktop/src/lib/local-preview.ts` | 修改 | DFM 自动展示只归一化路径/元数据，不在 localhost remote-gateway 模式下先下载数十 MB 的 HTML 正文。 |
| `apps/desktop/src/store/preview.test.ts` | 修改 | 覆盖 runtime session ID 与持久会话 ID 不同时，最终 HTML 必须登记到可恢复的持久会话并保持预览打开。 |
| `apps/desktop/src/app/session/hooks/use-preview-routing.test.tsx` | 修改 | 覆盖升级前已误登记在 runtime ID 下的报告仍可按当前会话别名恢复，无需重跑分析。 |

该增量复用 Desktop 现有 `setSessionPreviewTarget` 和 HTML webview，没有新增 RPC、model tool、DFM action 或报告格式。数据边界是 `dfm_analysis` 已返回的最终报告描述，不读取 observations、Runtime 或 HTML 内容，因此不使 Desktop 与 DFM 分析内部实现耦合。

## 既有实现复用校验

| 源文件 | 集成目标 | SHA-256 |
|---|---|---|
| `DFM-HTML/generate_html_report.py` | `tools/dfm/reporting/html/template.py` | 以该文件为基线纳入；集成版额外完成中文微标题本地化，因此哈希不再相同 |
| `DFM-HTML/vendor/three.r128.min.js` | `tools/dfm/reporting/html/vendor/three.r128.min.js` | `9274BBCEC8D96168626C732B5D31C775AA8CFB7EAA0599BEC0C175908A2C1CE2` |
| `DFM-HTML/vendor/OrbitControls.r128.js` | `tools/dfm/reporting/html/vendor/OrbitControls.r128.js` | `02BB4ADE710F3E607329E37A21F098BC3AC70EB6E33DAF8A65E79F4DB785E7B2` |

原始 `DFM-HTML/` 目录没有被修改，不计入改动数，生产代码和新增测试也不依赖该未跟踪目录。

## 验证记录

- 6 项 HTML reporting 针对性检查通过。
- result assembler 和 skill contract 针对性检查通过。
- Python 语法编译检查通过。
- `git diff --check` 通过；只有行尾格式提示。
- 当前环境没有安装 `pytest`，未执行完整 pytest 测试集。

2026-09-08 单次语义来源纠偏后的增量验证：

- 4 项 Runtime adapter/HTML wrapper 直接测试通过；
- 1 项 Runtime adapter 到 HTML renderer 的组合测试通过；
- 1 项 drawing context/Observation/Fusion 集成测试通过，包含 `available_pages` 断言；
- 3 项 DFM Skill 契约测试通过；
- 修改涉及的 Python 文件全部通过 `py_compile`；
- `.venv` 未安装 `pytest`，因此以上测试通过直接调用测试函数执行，尚未运行完整 pytest suite。

2026-09-08 成功条件纠偏后的增量验证：

- Python 语法编译检查通过；
- worker 报告门禁冒烟测试通过：HTML Runtime 完成后为 `reporting/report_editing/98%`；
- service 报告闭环冒烟测试通过：`reporting` 时 `result` 被拒绝，`report_context` 返回完整内联 Runtime，HTML 写入后转为 `succeeded/complete/100%`；
- 当前环境仍未安装 `pytest`，尚未执行完整 pytest suite。

2026-09-08 Desktop 自动展示增量验证：

- `dfm-viewer-events.test.ts` 针对性测试 5/5 通过；
- Desktop TypeScript typecheck 通过；
- 相关文件 Prettier 检查通过；
- Desktop ESLint 通过（0 errors；113 个既有 warnings）；
- 相邻 jsdom 回归测试 15/16 通过；唯一失败为未修改的 `use-preview-routing.test.tsx` 对 `localStorage` 空值的既有断言，单独运行同样失败，与本增量无关。

2026-09-08 Desktop 真实 E2E 自动展示纠偏：

- 真实 `render_html` 返回已核验为 `run.status=succeeded`、`report.kind=report_html`，且 31.96 MB `report.html` 存在；
- Desktop 在 `HERMES_DESKTOP_REMOTE_URL=http://127.0.0.1:9120` 模式下出现文件 API 15 秒 timeout，确认原预览 enrichment 会在打开前读取完整 HTML；
- DFM 自动展示改为跳过正文 enrichment，仅通过 Electron 本地 IPC 归一化路径和文件元数据，再由既有 HTML webview 直接加载。
- 第二次真实复测确认 `tool.complete` 使用 runtime session ID，而预览恢复优先使用持久会话 ID；旧接线将报告登记在 runtime ID 下，随后恢复 effect 按持久 ID 查不到记录并立刻清空面板。当前接线通过 `setCurrentSessionPreviewTarget` 统一登记到当前持久会话，避免自动打开后被清除。

## 未计入范围

仓库原先存在的日志、临时脚本、数据集、`tools/dfm/reporting/pptx_baseline.py` 和其他未跟踪文件均未修改，也未计入上述数量。

## Molex 343450001 完整 Hermes Agent E2E（HTML 链路验收）

运行日期：2026-09-08

这次由 Hermes CLI oneshot 自行调用 `dfm_project` / `dfm_analysis`，模型为阿里云 DashScope `qwen3.5-plus`。输入只有 Molex PDF/STEP 与用户已确认的 E2E 测试覆盖值；中间 observation、Runtime、`llm_content` 和 HTML 均由 Hermes 工具链生成，没有人工写入或补齐 artifact。

后续轨迹复核限定：该 oneshot 的用户提示明确要求“禁止在 `report.html` 生成前结束”，因此 Agent 连续轮询后台 run，而不是走普通非阻塞交互。它尝试通过不可用的 terminal 工具读取 Runtime 两次，第一次 `render_html` 又因 issue ID 不匹配失败，随后从服务端校验错误取得正确 ID 并重试成功。因此这次运行证明真实 Agent 可以驱动整条技术链路，但不能证明旧状态机能够稳定自动收尾；成功条件纠偏正是为消除这个缺口。

```text
PDF + STEP -> OCR -> Hermes observations -> Plan -> OCCT C++ Run
-> runtime_data.jsonl -> 同一 Hermes 会话最终总结
-> llm_content.jsonl -> render_html -> report.html
```

| 项目 | 值 |
|---|---|
| Project | `dfm_54a9bb13f2cb464a` |
| Plan | `plan_84ae728c38db4032` |
| Run | `run_db6b65ede57940d9` |
| Hermes session | `20260908_100130_971584` |
| Analyzer | `occt_cpp@occt-dfm-geometry-1.4.2` |
| 状态 | `succeeded / complete` |
| Run artifacts | 30 |
| 测量 / 评估 / 未通过 | 3 / 1 / 1 |
| Observation / `global_note` | 9 / 4 |
| PPT | 0 |

| Artifact | 字节数 | SHA-256 |
|---|---:|---|
| `drawing_d91c3c288a75e5b2_agent_observations.jsonl` | 6,526 | `f0c224365d78e43ea4de26a6dbb3b6aae7475ecd3ada8d25bfa65b8eedd92723` |
| `dfm_report.json` | 1,865 | `9f36b94c6d305e228f791ada617d7c63a4bd539cd01eaa18228e3f611a8f1577` |
| `dfm_report.md` | 442 | `78bec9a9d5f1df3115c80c7399b6e621bf18c850ebdd11f4ee670570922b0da5` |
| `runtime_data.jsonl` | 8,558 | `c00c0738f80ec9515af04416e57c53363aa8820203ab96d801650458cf04e7d6` |
| `llm_content.jsonl` | 1,160 | `3354ee48b9fa9be32c7573184ef3ec3c7132c0fdd15f0c806bba41576c0c0411` |
| `report.html` | 31,960,994 | `0bc8e6cbabb48f3636fdcf7ec07d0f8f4baf9d0a4b103e60fe977354baeb1cfa` |

验收结论：HTML 主 pipeline 已真实跑通；`llm.issues[].issue_id` 与 Runtime issue ID 完全一致，HTML 内嵌本次 3D/标量场、12 张证据图和 PDF，运行目录没有 PPT。PDF/BOM 材料为 PBT，本次为验证当前 ABS 规则链使用 `material=ABS`、`model_units=mm`、`pull_direction=[0,0,1]` 测试覆盖值，不是正式 PBT 工程结论。

内容限定：用户决定本次先验收 HTML pipeline，暂不将 observation 提取准确性作为本 commit 的阻塞项。这次 Hermes 仅持久化 4 条 `global_note`；同份 OCR fragments 中还有 14 条本应归入 `global_note` 的 Note 内容未被第一次语义整理保存。这是已知的后续完整性校验任务，不是 Runtime-to-HTML 映射丢失。

产物路径：

```text
C:\Users\abc\.hermes\workspace\dfm\projects\dfm_54a9bb13f2cb464a\runs\run_db6b65ede57940d9\artifacts\report.html
```

## Molex 343450001 Service 直接集成校验（非完整 Hermes Agent E2E）

运行日期：2026-09-08

输入：

```text
D:\hermes_deployment\samples\molex\343450001\343450001.step
D:\hermes_deployment\samples\molex\343450001\343450001.pdf
```

后台 Hermes oneshot 首先创建了全新项目并完成输入/OCR discovery，但当前配置的 `copilot/gpt-4o` 在仓库目录运行时因开发上下文与四页 OCR 叠加达到 38,727 tokens，未能完成 observations；从样本目录重试后又停在首次语义整理模型调用。保留这些失败项目用于诊断，没有将其错误 observations 用作报告输入。

随后由当前开发 Agent 继续驱动同一套正式 `DFMService` 接口：逐页读取 `drawing_context`，通过 `submit_observations` 完成服务端 fragment ID、schema、confidence 和 revision 校验；再执行 discover/fusion/plan/start/result 和正式 `render_html` action。它验证了 service 和 renderer 的接口，但最后英文 `llm_content` 是为技术接线而机械组装，不是 Hermes 报告编辑模型自主完成，因此不作为完整 Agent E2E 验收证据。

| 项目 | 值 |
|---|---|
| Project | `dfm_aa46a164fce549da` |
| Plan | `plan_3c929ec6a20f4a4b` |
| Run | `run_a543d8f0448c41f1` |
| Analyzer | `occt_cpp@occt-dfm-geometry-1.4.2` |
| 状态 | `succeeded / complete / 100%` |
| Observation | 32 |
| `global_note` | 18 |
| Run artifacts | 30 |
| 测量 / 评估 / 未通过 | 3 / 1 / 1 |
| PPT | 0 |

数据链核验：

- `drawing_observations` JSONL、`runtime.drawing_semantics.observations` 和本次 Plan 的 DiscoverySnapshot 均为 32 个相同 observation IDs；
- `llm_content` 包含全部 18 条持久化 `global_note`，遗漏数为 0；
- `llm.issues[].issue_id` 与 `runtime.report.issues[].id` 完全一致；
- HTML 包含零件标题、issue ID、Three.js、内嵌 PDF，以及 AS-33472-100、QEHS-699000-300、ISO 13715、R0.2 和 0.15 raised lettering 等 Notes；
- 运行目录没有 `.pptx`。

| Artifact | 字节数 | SHA-256 |
|---|---:|---|
| `drawing_d91c3c288a75e5b2_agent_observations.jsonl` | 29,361 | `a0726241b806808d0a2647853fe10d1130a1e708035e350c849957df66a03a8e` |
| `dfm_report.json` | 1,865 | `1e8e60a4d11943fa5b4dde6ca8ca72fc64ab3d640523c0cd99a732e12df592fc` |
| `dfm_report.md` | 442 | `78bec9a9d5f1df3115c80c7399b6e621bf18c850ebdd11f4ee670570922b0da5` |
| `runtime_data.jsonl` | 30,545 | `35fba02dfbb0341f434d0588a3112e34af5a8ae212d691cebf173c9b23bbbd46` |
| `llm_content.jsonl` | 2,067 | `f5574fdeb45c22dbce91757f4d9e719575adfbb31612f0b658ee2094dfe0f60c` |
| `report.html` | 31,961,919 | `548acaa949030e12de1c83fa5a2a908fce14b318659ebfe8fd2e4a3951cd3800` |

工程限定：PDF/BOM 的真实材料是 PBT；为只验证当前发布的 ABS 规则链，本次 confirmed material 使用 ABS 测试覆盖值，拔模方向使用 `[0,0,1]` 测试假设，因此结果不能作为该 PBT 零件的正式工程结论。

OCR 缺口：PDF 原生文字层包含 `SEAL PLUGS ARE NOT TO BE USED TO REPLACE SHORTING BAR TERMINALS.`，但当前栅格 OCR 的 `drawing_ocr_fragments` 中没有该行，所以本次 18 条 Notes 中不包含它。该遗漏发生在 OCR artifact 生成前端，不是 observation-to-Runtime 或 Runtime-to-HTML 丢失。

## Molex 343450001 历史 E2E 记录（架构纠偏前）

运行日期：2026-09-07

实际样本路径为 `D:\hermes_deployment\samples\molex\343450001`。用户消息中的 `D:\hermes\_deployment\...` 路径在机器上不存在。

OCCT 主 run 通过后台 Hermes Agent 和真实 `dfm_project` / `dfm_analysis` 工具完成。该次 run 的第一次 Hermes 图纸解释只持久化了 1 条 Note；为了验证 HTML，最终报告文案曾由当前会话模型直接根据已落盘 OCR/Runtime 生成，并通过正式 service `render_html` 入口提交。这个结果证明了 OCCT 到 HTML renderer 的技术通路，但因为报告阶段重新解释了 OCR，**不再作为 2026-09-08 单次语义来源架构的合格 E2E 证据**。新架构需要从 Discovery 阶段重新运行，让 Hermes 一次性生成完整 `drawing_observations`，再验证 Runtime/HTML。

| 项目 | 值 |
|---|---|
| Project | `dfm_7c3a696af33c4209` |
| Plan | `plan_41650e5bdcc5450e` |
| Run | `run_d5d0abd57db54dbb` |
| 工艺 | `injection` |
| Scope | `injection.default@1.1.0` |
| Analyzer | `occt_cpp` |
| 最终状态 | `succeeded` / `complete` / `100%` |
| Artifact 数量 | 30 |
| 测量 / 评估 / 未通过 | 3 / 1 / 1 |
| PPT 数量 | 0 |

### 测试输入说明

- STEP 文件头和 PDF 标题栏均明确表明单位为 `mm`。
- PDF BOM 的真实材料为 PBT；当前发布 ontology 的主体壁厚规则只覆盖 ABS，PBT 会以 `ontology_rule_not_found` fail-closed。
- 为验证 HTML 集成的完整 smoke path，本次把材料明确设为 `ABS` 测试覆盖值，并使用 `[0,0,1]` 作为拔模方向测试假设。
- 因此，本次产物证明真实 OCCT/报告/HTML Pipeline 可以跑通，但不能作为该 PBT 零件的工程结论。

### 最终报告产物

公共目录：

```text
C:\Users\abc\AppData\Local\hermes\workspace\dfm\projects\dfm_7c3a696af33c4209\runs\run_d5d0abd57db54dbb\artifacts
```

| Artifact kind | 文件 | 字节数 | SHA-256 |
|---|---|---:|---|
| `report_json` | `dfm_report.json` | 1,865 | `e067880bd4c268fd6defff944019518f90cc49544d6d67972dacfcc4a534c408` |
| `report_markdown` | `dfm_report.md` | 442 | `78bec9a9d5f1df3115c80c7399b6e621bf18c850ebdd11f4ee670570922b0da5` |
| `report_html_runtime` | `runtime_data.jsonl` | 1,979 | `1f67107d65001f380497d31085b927d3def4ec2d826af9518326a092dc1dc01d` |
| `report_html_llm` | `llm_content.jsonl` | 2,380 | `8b4cca078aaa73802fc1685c60c1f27a3fcf76a12bd10e950e8190bd4500ae08` |
| `report_html` | `report.html` | 31,962,248 | `11d001ede483915b476c5d2bfb0fd13a779182737f4ae5fe4c9b9c431502098c` |

核验结果：

- 五个报告 artifact 的文件大小及 SHA-256 均与 manifest 一致。
- `runtime.report.run_id`、`dfm_report.json.run_id` 和 manifest run ID 一致。
- `llm.issues[].issue_id` 与 `runtime.report.issues[].id` 集合一致。
- scene、draft/thickness scalar field、evidence geometry、PDF 和 rule snapshot 路径全部存在。
- HTML 已内嵌 Three.js、OrbitControls、PDF 和本次 run 数据。
- HTML 文案已包含通用公差、ASME Y14.5M-2009、ISO 13715、QEHS-699000-300、R0.2、凸字高度和其他 page-1 Notes。
- 最终目录不存在 `.pptx` 文件。
