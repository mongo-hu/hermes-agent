# DFM 二维图纸语义裁剪管线

该目录只负责确定性的二维图纸渲染、裁剪校验和高清区域渲染，不单独连接大模型，也不直接生成
`ObservationRecord` 或 `FusionLinkRecord`。

## 职责边界

输入支持 PDF、PNG、JPG/JPEG。页面预览和裁剪图只作为当前 Hermes 会话的
多模态 tool result 在内存中传递，不写入项目 Artifact。管线只持久化两类可追溯数据：

- `drawing_regions`：源文件哈希、页码、归一化 bbox、区域类型和稳定 region ID；
- `drawing_diagnostics`：页数、渲染器版本和处理诊断。

裁剪规划和事实整理都复用当前 Hermes Agent event loop 中已经配置的大模型：

```text
二维文件
→ 程序化低清整页渲染（内存）
→ drawing_context（全部页面低清预览）
→ Hermes Agent 提议完整语义区域坐标
→ submit_crop_plan
→ 程序校验/补边/文本块吸附并以内存方式返回高清裁剪图
→ Hermes Agent 整理尺寸、材料、技术 Notes 等明确事实
→ submit_observations
→ 程序校验 region ID、Schema 与 Revision 后落库
→ fusion_context（Observation + Feature + Region）
→ Hermes Agent 提议 2D/3D 关联
→ submit_fusion_links
→ 程序校验 ID 和 Feature/Region 关系，几何算法验证拓扑
→ 保存 candidate/ambiguous FusionLink
```

Agent 不能创建输入、Observation、Feature 或 CAD Region 标识，不能把 FusionLink 直接设为
`confirmed`。没有可靠语义或关联时应提交空数组；像素位置不能直接充当 CAD
`GeometryRef`。

## 模型和凭据

此管线没有 OCR 引擎，也没有独立的模型、Endpoint、API Key 或超时配置。两轮视觉
推理都由当前 Hermes 会话模型完成，从而避免重复路由、重复计费和两套模型治理。
当前模型必须支持原生视觉输入和多模态 tool result；否则管线明确停止，不能降级为
猜测或再次启用 OCR。
