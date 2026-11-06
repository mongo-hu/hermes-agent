# 审核状态、确认事务与 Agent 续接

适用于页面确认、`clarify`、`dfm_analysis` 和 Desktop 审核视图。

## 状态读取

`ManifestStore.load()` 是已提交项目的读取入口。`project_manifest.json` 是检查点，可能落后于 `recovery/manifest.events.jsonl`；读取检查点不能代表当前项目状态。服务读取时回放已提交事件，并通过文件签名使跨进程缓存失效。

Desktop 的审核端点继续提供完整几何，供选面和编辑使用。Agent 的 `review_context`、确认回复及其它审核编辑回复调用 `DFMService.review_state()`，从同一已提交版本返回：

- `revision`、`snapshot_id`、完整特征计数、按类型和状态划分的审核组计数；
- MAIN 的明确确认状态，以及待审组、业务输入的数量；
- `next_call`：下一次工具调用及参数，不需要模型从几何或磁盘文件推测流程；
- `review_groups`：最多八组的索引页，不内联网格、面列表或数值场；
- `review_page.next_cursor`：绑定版本的续页游标，版本变化后要求重新查询。

`review_status` 可选择 `pending_review`、`confirmed` 或 `all`。`review_group_ids` 可查询最多八个明确组，忽略状态过滤。索引的计数覆盖全部组，分页不改变计数；回复控制在 6500 字符以内，适用于未创建终端环境的 Agent。

视图刷新使用 `phase=discovery`、`viewer_manifest` 和已提交版本。`status` 表达业务下一步，允许同时存在“普通结构已确认”“局部方案待审”“业务输入待答”，不能拿单个业务状态判断是否刷新模型。

## 确认与续接

不传组 ID 的确认只确认普通结构。局部方案必须显式指定组，不能在普通结构已经确认后自动确认仅剩的局部方案。

在当前版本或当前几何快照下，重复确认未改变内容的已确认组返回 `unchanged=true`，不追加审核版本。不存在、已撤销、内容变化的组仍拒绝；过期版本或不同几何快照仍要求刷新。

普通结构确认后，先执行 `next_call` 中的业务澄清。未完成局部方案保持待审；准备就绪的计算继续遵循已有依赖规则。已经启动的 Run 继续原 Run，不因重复确认新建项目。

## 保存失败的分类

| 阶段 | 结果 | 是否建立保存阻断 |
| --- | --- | --- |
| 参数、版本、快照、审核组校验 | `DFMError`，请求未被接受 | 否 |
| 几何编辑意图已接受，Worker 或提交失败 | `ReviewSaveError`，保留原始原因和 edit_id | 是 |
| 校验成功，写入事件日志失败 | `ManifestCommitError` | 是 |
| 审核已提交，后续派生视图发生错误 | 已提交状态保持有效 | 否 |

真正的保存失败仍需原操作成功重试才能解除，不能用其它组的成功写入清除。

旧版本误记的 `review_group_stale` 仅在证据充分时恢复：对应操作必须是确认；目标组全部已确认；没有未完成或失败的几何意图；该错误没有新分类的真实保存失败标记。恢复使用正常事件提交，并保留 `review_save_recovery.previous_failure`，不手工删检查点字段。

## 验证

- `test_review_state_contract.py`：485 组大结果、完整分页、检查点落后、版本变化、历史阻断恢复条件。
- `test_native_review_continuation.py`：真实 Worker 的确认、重复提交、版本拒绝、写入故障和提交后视图故障。
- `test_desktop_agent_review_continuation.py`：真实 Electron 页面、Gateway、AIAgent、工具和 Worker。只有外部模型提供方使用本地确定性响应；下一次调用从真实工具结果读取。断言页面提交后业务输入显示、界面确认计数更新、无截断和错误保存阻断。

Python 测试统一通过 `scripts/run_tests.sh`。桌面验收需设置 `DFM_DESKTOP_E2E=1` 和 `DFM_GEOMETRY_E2E_EXECUTABLE`，并预先构建 Desktop。

## 重启后的状态一致性

识别时将经过协议校验的几何能力保存到项目的 `geometry_discovery.geometry_capability`。业务问题查询使用该证据创建独立的本体查询视图，不依赖进程全局映射，也不改变其它项目的查询能力。旧项目缺少这份证据时才进行一次惰性握手；握手失败必须作为依赖错误返回，不能解释成零项适用检查。

当前快照通过已提交输入、事实、结构、成型域和原快照的提供方版本计算并校验。状态查询不再调用识别刷新，不因重启产生第二套候选或启动 Worker。能力是否能执行，仍由计划/执行阶段的真实握手校验。

计划摘要保留 `waiting_input` 的业务分类、待答数量及精确 `next_call`；局部方案缺少人工审核时显式等待审核，不能转换成终止或重复生成计划。分片计划按 `plan_fragments` 描述，不要求已删除的旧 `operations` 字段。

`review_context` 同时携带已提交的视图路径和版本，使成功读取可以更新页面并取代历史失败提示。新增冷启动测试覆盖重建服务后的状态读取、第一次计划调用和页面确认后重建服务再续接。
