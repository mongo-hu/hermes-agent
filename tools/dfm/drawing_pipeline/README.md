# DFM 二维图纸 OCR 管线

该目录只负责确定性的二维图纸预处理与 OCR，不单独连接大模型，也不直接生成
`ObservationRecord` 或 `FusionLinkRecord`。

## 职责边界

输入支持 PDF、PNG、JPG/JPEG。管线输出三类可追溯 Artifact：

- `drawing_raw_text`：按页保存的 OCR 原文；
- `drawing_ocr_fragments`：带稳定 `fragment_id`、页码、bbox 和置信度的 NDJSON；
- `drawing_diagnostics`：OCR Provider、版本和降级诊断。

OCR 完成后的语义理解复用当前 Hermes Agent event loop 中已经配置的大模型：

```text
二维文件
→ 程序化 OCR
→ drawing_context（有界 OCR 片段）
→ Hermes Agent 判断尺寸、材料、皮纹等语义
→ submit_observations
→ 程序校验 fragment_id、Schema、置信度与 Revision 后落库
→ fusion_context（Observation + Feature + Region）
→ Hermes Agent 提议 2D/3D 关联
→ submit_fusion_links
→ 程序校验 ID 和 Feature/Region 关系，几何算法验证拓扑
→ 保存 candidate/ambiguous FusionLink
```

Agent 不能创建输入、Fragment、Feature 或 Region 标识，不能把 FusionLink 直接设为
`confirmed`。没有可靠语义或关联时应提交空数组；像素位置不能直接充当 CAD
`GeometryRef`。

## 模型和凭据

此管线没有独立的模型、Endpoint、API Key 或超时配置。模型选择和凭据沿用 Hermes
当前会话配置，从而避免重复路由、重复计费和两套模型治理。
