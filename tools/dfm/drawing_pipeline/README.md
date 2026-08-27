# DFM 二维图纸解析管线

## 职责边界

Drawing Pipeline 是 Hermes DFM Discovery 的底层文档解析模块，负责：

- PDF、PNG、JPG、JPEG 的 OCR；
- 保留 OCR 页码、bbox、原文片段和置信度；
- 在独立模型调用中提取材料、公差、表面要求和制造备注候选；
- 返回 `DrawingPipelineResult`，不读取项目规则，不决定 Fact 和 DFM 结论。

Hermes 侧 `DrawingAnalyzer` 负责将候选转换为正式 `ObservationRecord`，并生成三个可追溯制品：

- `drawing_observations`：`application/x-ndjson`，每行一条正式 Observation；
- `drawing_raw_text`：OCR 原文，只作为受控 Artifact 保存；
- `drawing_diagnostics`：依赖、模型调用、警告和数量统计。

## Discovery 数据流

```text
InputRecord(drawing)
→ Drawing Pipeline
→ DrawingCandidate[]
→ DrawingAnalyzer
→ ObservationRecord[] + ArtifactRecord[]
→ source_policy
→ confirmed Fact / needs_confirmation / conflict
→ 3D FeatureRecord + RegionRecord
→ FusionAnalyzer.resolve
→ candidate / ambiguous FusionLinkRecord[]
→ DiscoverySnapshotRecord
```

`FusionAnalyzer` 只解析 Observation 与 Feature/Region 的可审核关系，不启动 STEP Analyzer，也不把
二维文字当作三维客观测量。全局材料、公差等信息默认不建立局部 FusionLink。

## 错误和降级

- Drawing Pipeline 失败时抛出带稳定错误码的异常，不输出伪成功 JSONL；
- 纯二维项目失败会显式阻塞；
- 2D+3D 项目允许三维主链路继续，但降级原因写入 Manifest 的 `drawing_discovery` capability；
- LLM 语义提取失败会记录 warning，已经完成的 OCR 原文仍可追溯；
- 原文不会作为“等待模型处理”的 Observation 值写入项目。

## 配置与依赖

二维依赖属于 `[dfm]` 可选依赖组：

```toml
dfm = [
  "python-pptx==1.0.2",
  "rapidocr-onnxruntime>=1.4.0",
  "pymupdf>=1.20.0"
]
```

模型、Base URL 和超时配置在 `config.yaml` 的 `dfm.drawing` 下；API Key 属于凭据，继续使用 secret
存储或 `.env`。DWG 尚未纳入正式输入格式，避免将商业 CAD SDK 变成基础依赖。
