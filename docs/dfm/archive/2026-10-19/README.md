# DFM 历史方案与评审记录

更新：2026-10-19。[开发导航](../../README.md)

这里只保留 4 份有独立追溯价值的历史记录，不再保存每篇现行文档的完整副本。它们说明当时如何设计和验证，不是当前开发规范或最新测试结果。

| 历史记录 | 保留理由 | 当前维护位置 |
| --- | --- | --- |
| [dfm-catalog-contract-alignment.md](dfm-catalog-contract-alignment.md) | 仅作历史契约和测试记录；旧 Metric／Quantity 路由及规则选择方式不用于新 Worker。 | [现行规范](../../dfm-rule-catalog-database-design.md) |
| [注塑件 DFM 清单分类与技术覆盖评审 V1.0.md](%E6%B3%A8%E5%A1%91%E4%BB%B6%20DFM%20%E6%B8%85%E5%8D%95%E5%88%86%E7%B1%BB%E4%B8%8E%E6%8A%80%E6%9C%AF%E8%A6%86%E7%9B%96%E8%AF%84%E5%AE%A1%20V1.0.md) | 保留分类取舍、几何／物理结论边界与未采纳方案的评审依据；其中旧路由和几何适用条件建议不等于现行契约已经支持。 | [现行规范](../../dfm-injection-requirements-and-coverage.md) |
| [2026-07-13-dfm-hermes-agent-development-roadmap.md](plans/2026-07-13-dfm-hermes-agent-development-roadmap.md) | 保留早期里程碑、范围取舍与验收门槛；其中完成状态和 Region 关系属于当时基线，不作为当前任务指令。 | [现行规范](../../%E6%B3%A8%E5%A1%91%E4%BB%B6%20DFM%20%E7%89%B9%E5%BE%81%E8%AF%86%E5%88%AB%E4%B8%8E%E5%87%A0%E4%BD%95%E8%AE%A1%E7%AE%97%E5%BC%80%E5%8F%91%E4%BB%BB%E5%8A%A1%E6%B8%85%E5%8D%95%20V1.0.md) |
| [2026-08-18-dfm-architecture-workflow-and-occt-contract.md](plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md) | 保留一次性 Discovery／Objective 协议及早期跨仓职责的设计背景；不作为常驻会话调用依据。 | [现行规范](../../dfm-geometric-session-contract.md) |

## 本次清理依据

- 删除 8 个旧跳转入口，现行文档及保留历史记录中的链接直接指向维护位置。
- 删除 11 份重复快照：主流程、会话、规则、部署、运行、系统架构、工程清单、覆盖矩阵、A 类映射、任务清单，以及仅作跳转的 NX 旧页。
- 删除清单和任务副本前，核对 171 条原编号、全部逐行工程描述、A/B/C/D 分类与条件边界、逐行任务关联，以及 48 个任务包；A 类映射的来源行索引也一致。
- 未完全合并的 Citation／criterion_id 迁移、历史 warning 的数据库约束前提并入现行规则文档；计划失效及 Viewer 回退诊断补充到运行手册。
- 历史测试失败和未部署范围继续保留，不改写为已经通过。产品规划、合作筛选资料和原展示素材未删除。

删除的文件包含未提交副本，不假定可从 Git 完整恢复。本次在系统临时目录保留了清理前的安全副本和恢复清单；它不属于仓库文档，可能随系统临时文件清理而消失。后续正式追溯以提交到版本库的现行规范及上述历史记录为准。
