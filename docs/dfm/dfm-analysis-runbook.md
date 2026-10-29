---
title: "DFM 运行数据与故障排查"
status: implementation-reference
updated: 2026-10-29
type: runbook
---

# DFM 运行数据与故障排查

[开发导航](README.md) · [安装和 doctor](dfm-deployment-environment.md)

本文说明现有实现的数据入口和排查方法，不定义新的分析流程。总体流程见[主流程](dfm-main-workflow-explained.md)，常驻会话与增量编排按[会话接入设计](dfm-geometric-session-contract.md)实施。

核对日期：2026-10-29；当前工作区接入 dfm.geometry/v2 / bundle 2.3.0，真实 Worker 和 Electron 两轮逐组确认到异步 HTML 演示已联调。具体范围见会话文档第 10 节；自然语言聊天全场景和正式发布包尚未验收。排查时仍须记录实际 Hermes 源码、Worker 构建、规则发布身份和样本，不能假定用户安装目录已经更新。

## 1. 先确认排查的是哪次运行

| 标识 | 从哪里取 | 用途 |
| --- | --- | --- |
| `project_id` | 工具应答、项目 Manifest | 定位长期项目，不等于聊天会话 |
| `plan_id` | Manifest 的计划记录 | 查看实际选择的检查、输入和规则 |
| `run_id` | start／status 应答 | 定位此次执行、日志与制品 |
| 输入 SHA256 | InputRecord | 避免把同名文件的不同版本混在一起 |
| 本体发布身份和哈希 | Plan 与本地本体元数据 | 说明使用哪份规则，不是 Worker 几何快照 |

同一项目可以有多次运行，不能仅凭“最新文件夹”判断用户正在查看的结果。优先使用工具应答中的 ID 和 `next_action`。

## 2. 数据保存在哪里

工作区随当前 Hermes profile 的 `HERMES_HOME` 隔离：

```text
<HERMES_HOME>/workspace/dfm/
├─ ontology/
│  ├─ dfm-ontology.sqlite3       # 当前安装的规则投影
│  └─ packages/                  # 远程下载的历史发布物
├─ projects/<project_id>/
│  ├─ project_manifest.json      # 状态检查点，不一定包含最新提交
│  ├─ recovery/
│  │  └─ manifest.events.jsonl   # 检查点之后的可靠增量提交
│  ├─ inputs/                    # 已登记的输入副本
│  ├─ artifacts/geometry_sessions/<identity>/
│  │  ├─ session-recovery.json   # 已提交几何状态的恢复线索
│  │  └─ ...                    # 共享显示网格、拓扑及识别制品
│  └─ runs/<run_id>/
│     ├─ events.jsonl
│     └─ artifacts/              # G 批次、规则结果、定位、Viewer、报告
├─ tmp/
└─ .locks/
```

目录为现有实现的定位参考，不保证每次运行生成所有文件。具体位置以 Manifest 的 `ArtifactRecord.relative_path`、种类、大小和哈希为准；相对路径以项目目录解析，不以当前终端目录解析。

读取当前项目应使用 ManifestStore.load()，它从进程内已提交状态或检查点加日志恢复，不能只打开 project_manifest.json 推断最新确认。原生 OCCT 对象仅在 Worker 内存中；Hermes 内存保存其语义投影、工作流状态、受限的小型 G/判定工作表及少量制品摘要。SQLite 查询本体规则，不存本轮 DFM 会话；不依赖 Redis。Viewer 的 scene_path/topology_path 则相对于 Viewer 文件自身所在目录解析，与 ArtifactRecord 的项目相对路径不要混用。

Desktop 附件路径只是输入来源。登记后应使用项目 `inputs/` 副本和哈希，不依赖聊天临时附件长期存在。相同内容可复用，替代输入和失效计划保留追溯关系。

## 3. 现有工具如何继续运行

不要将这里的 action 名称等同于 Worker RPC 方法：前者是 Agent 工具，后者是几何进程接口。

| 工具返回的阶段 | 后续处理 |
| --- | --- |
| 有待确认事实 | 使用绑定项目与事实名的交互澄清，用户答案由程序保存；不由 Agent 猜测或直接调用 confirm_fact 代答 |
| `agent_interpretation_required` | drawing_context → 使用当前模型整理原文 → submit_observations |
| `agent_fusion_required` | fusion_context → 提议可靠关联 → submit_fusion_links；保留歧义 |
| `discovery_review_required` | 在三维界面审核／修订，使用绑定项目的确认交互；完成后遵循 next_action |
| 已具备有效 Discovery | 按 next_action 生成计划并 start；不要重复创建项目或登记同一附件 |
| `reporting` | 同一 project_id／run_id 获取 report_context，准备内容，再提交 render_html |
| `succeeded` | result 获取已登记的最终报告及制品 |

当前 `tools/dfm_tool.py` 的工具约束和 `service.py` 的 action 实现是调用依据。OCR／融合提交使用 Context 返回的来源 ID 和 revision，冲突时重新读取，不覆盖后来的修订。

当前外部链路由 runtime/process.py 管理 `serve --stdio`，通过 snapshots.open、features.discover、geometrics.compute 等 RPC 工作；capabilities CLI 仅供独立探测。模型及不可变快照在健康 Worker 中复用，磁盘 JSON 本身不能恢复原生 OCCT 对象。进程退出后，Hermes 根据已提交恢复状态重新导入并重建逻辑状态，旧进程句柄不能直接使用。

## 4. 用哪些文件回答哪些问题

| 数据 | 回答的问题 |
| --- | --- |
| ManifestStore.load() 的已提交投影 | 当前项目输入、确认事实、特征审核、Plan、Run 和 Finding 是什么；磁盘检查点需加恢复日志 |
| Plan／Run 的 `plan_snapshot` | 当时固定了哪份输入、规则、参数和计算操作 |
| Plan 的片段及 G 批次结果 | 发送了哪些 Feature/G/context，结果绑定到哪些来源片段 |
| `events.jsonl`、Run 进度与错误 | 执行停在哪个阶段，是否完成／取消／失败 |
| Worker 异常中的 stderr_tail、Hermes 日志 | 子进程异常、DLL、编码与消息解析故障；不保证每个 Run 都有独立 stdout/stderr 文件 |
| `geometric_results.json`、`geometric_batch_*.incremental.json` | 当前累计客观结果及不可变的各批次结果；累计表按身份替换，不重复叠加 |
| `evaluations.json` | 哪条规则、哪些变量、什么数值和判定结果 |
| `scalar_*.json` | 厚度、角度等局部场及有效采样 |
| `render_scene.json`、`topology_map.json` | 显示模型与 B-Rep 面如何对应 |
| `evidence_geometry.json`、`evidence_records.json` | 问题与几何位置、测量、图片的关联 |
| Viewer 制品 | 前端显示哪些特征、问题和高亮；种类及路径看 ArtifactRecord |
| HTML Runtime、`llm_content.jsonl`、`report.html` | 报告输入、Agent 内容与最终 HTML；报告未完成时不保证均存在 |

JSON／MD／PPTX 等其他交付以实际登记的 artifact 为准。不要依赖固定图片数量或旧文件名，也不要将未登记的中间文件当作正式结果。

### 增量计划的诊断补充

G 片段记录所需确认组、参考关系、输入和状态；几何修订产生替代片段，不改写已派发片段。已启动 Run 在确认或输入提交后自动重新规划和续算，只影响依赖闭包；等待某个局部方向不占用执行线程，也不阻塞其它就绪 G。历史结果保留，但当前规则与 Viewer 只选择未被替代片段的有效结果。InputRecord、PlanRecord、RunRecord 的完整字段以 tools/dfm/contracts.py 及对应 Schema 为准。

Viewer 的 issues[].triangle_refs 可缺失：只有面级 geometry_refs 时应展示范围定位，不能声称精确局部高亮。图片预算不等于问题数量，未出图或未进入渲染预算的问题仍应保留在业务统计中。

## 5. HTML 为什么还没出来

`runtime/jobs.py` 准备确定性结果与 HTML Runtime；Agent 通过 `report_context` 取得完整输入和 `required_issue_ids`，提交内容后，`service._render_html` 校验问题 ID 并将渲染交给 JobManager。

```text
确定性结果就绪 → reporting → report_context → Agent 整理内容
  → render_html 接受任务 → 后台渲染 → 登记 report_html → succeeded
```

- 一直在 reporting：先看 next_action，区分正在等待 Agent 内容、队列中还是渲染中；不要重建项目。
- `report_content_invalid`：内容中的 issue_id 必须与 required_issue_ids 完整且唯一地对应，不新增或省略。
- Playwright／Chromium 不可用：按部署指南在启动 Agent 的同一解释器检查 `html_report.ready`。
- Worker completed 不代表报告完成；HTML 场景需要有效 report_html 登记后才算最终完成。
- 问题与已有高亮应先于报告发布；HTML 慢不能作为前端不展示问题的理由。

## 6. 问题数量和三维高亮如何核对

问题来源是未通过的业务 Check，不按技术指标数量或严重度计数。核对 `dfm_report.json.stats.issue_type_counts` 与 Viewer 的同类统计；未评估、失败计算和未覆盖项不能伪装成通过。

```text
Finding／issue → evaluation_id → 规则与 Operand
  → Measurement／ScalarField → 几何证据
  → topology_map + 匹配网格 → Viewer／HTML
```

只有范围证据时是初步定位；有合格的局部证据后才能声明精确区域。参考对象参与公式不代表参考对象本身违规，不能把参考主壁全部标红。

高亮不一致时，依次检查输入哈希、拓扑快照、显示网格快照、evaluation 关联和证据版本。面引用可映射到当前网格，旧 triangle_refs 不能直接套用新网格。静态截图也不能证明交互式定位正确。

识别 Viewer 的文件名为 dfm_discovery_viewer.json，预览/分析 Viewer 使用 dfm_viewer.json；当前 Electron 弹窗入口支持这两种名称并检查内容协议。若三维侧栏正常但识别结果弹出返回 invalid_viewer_manifest，应确认 Electron 主进程已重新打包，而不是只更新 Python。真实窗口验收命令见会话文档第 10.15 节。

## 7. 常用排查命令

在启动 Agent 的同一环境、同一 profile 下执行。PowerShell 中只读取实际记录指向的项目：

```powershell
python .\hermes dfm doctor --json
$dfmProfileRoot = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE '.hermes' }
$dfmWorkspaceRoot = Join-Path $dfmProfileRoot 'workspace/dfm'
Get-ChildItem -LiteralPath (Join-Path $dfmWorkspaceRoot 'projects') -Directory |
    Sort-Object LastWriteTime -Descending | Select-Object -First 5 FullName, LastWriteTime
python -c "from tools.dfm.project.manifest import ManifestStore; from pathlib import Path; print(ManifestStore(Path('<实际项目目录>')).load())"
Get-Content -LiteralPath '<实际Run目录>/events.jsonl' -Encoding UTF8
```

doctor 检查环境和能力，不证明某个 STEP 的工程计算已通过。先核对实际 EXE 的 bundle/build 身份，再核对项目采用的快照和结果来源；远程规则同步是否成功另查安装身份。

## 8. 失败与恢复

| 现象 | 先检查什么 | 处理原则 |
| --- | --- | --- |
| OCCT unhealthy／dependency_missing | 实际 EXE、运行库、Capability 和 stderr | 修复明确的后端依赖，不静默切换 PythonOCC |
| 规则数量少或没有更新 | endpoint、组织／工艺、pinned_snapshot_id、同步日志与安装身份 | 下载成功不等于已选中并安装；同步细节见规则契约 |
| 识别无结果或部分失败 | coverage、目标范围、诊断 | 未完成不等于数量为零，不直接认定没有风险 |
| 计划失效或输入改变 | revision、输入哈希、失效原因与受影响操作 | 按工具生成新计划或运行，不改写已完成结果 |
| 计算失败／取消 | Run 错误、events、stderr、已登记制品 | 保留诊断；部分文件不当作完整分析 |
| 报告失败 | reporting 阶段、内容校验、浏览器日志 | 从有效输入重试报告，避免无关几何重算 |

会话句柄过期时由进程池续租或按已提交恢复状态重建；重建失败应明确报错。人工修改存在 pending 意图而项目尚未提交时，不能采用 Worker 最后返回的分支。详细恢复和局部 stale 约束见会话文档第 10 节。

## 9. 数据保留与维护

输入、Manifest、历次 Run 和已登记制品是项目数据，不随聊天关闭而删除。临时文件清理由既有保留策略管理；不要手工改写运行中的 Manifest、事件或制品。

路径、状态和产物变化时同步更新本手册，并用真实 Run 核对。客户模型和日志可能包含知识产权，应限制访问、脱敏并按项目保留策略备份。
