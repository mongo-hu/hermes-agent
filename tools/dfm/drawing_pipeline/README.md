# DFM 2D 解析管线 (Drawing Pipeline)

## 整体架构变动简述：
### 整体调用流程
JobManager._execute() - FusionAnalyzer - DrawingAnalyzer - Drawing Pipeline - DrawingAnalyzer - FusionAnalyzer 

### 新增独立 2D 解析管线 (Drawing Pipeline)
作用：底层计算模块，负责对 2D 图纸执行 OCR/DWG 识别、LLM特征提取（LLM提取过程完全独立于Hermes，不污染Hermes上下文，也即是Hermes不存在原始txt文本信息，只有最终的结构化jsonl）
调用链条：由主进程中的 DrawingAnalyzer 直接调用其 interface.py 暴露的方法。
入参：file_path: str（图纸文件的绝对路径）。
出参：Tuple[str, str]（返回包含了标准 JSONL 格式字符串和原始 OCR 提取文本的元组。原始文本向上抛出，不在黑盒内部写盘，防止污染运行环境）。

### 重写 2D 数据流适配器 (DrawingAnalyzer)
作用：作为主系统与底层黑盒之间的桥梁（适配器模式）。它负责拆解主系统丢过来的上下文对象，拿到图纸路径喂给Drawing Pipeline；接着将底层产出的 JSONL 字符串与原始 raw_text 双双落盘为物理文件，随后对 JSONL 文件计算哈希、包装成主系统认得的取件凭据（ArtifactRecord），最终返回给上层。
调用链条：被上层的并发编排器 FusionAnalyzer（或在纯 2D 场景下被 JobManager）直接调用。
入参：context: AnalyzerContext（系统上下文对象，包含 project_dir、run_id 和已注册的 inputs 文件元数据列表）。
出参：List[ArtifactRecord]（仅向主系统返回包含 `drawing_observations.jsonl` 的正式制品记录）。

### 重写并发融合调度器 (FusionAnalyzer)
作用：3D+2D 混合场景下路由器。它内部维护了一个 ThreadPoolExecutor (容量为2)，负责不阻塞地同时拉起 3D 和 2D 两个引擎（单方完工会进行资源整合），并在双方完工后汇合数据。
调用链条：由系统核心后台任务管理器 JobManager._execute() 唤醒。它被唤醒后，向下同时调用 StepAnalyzer.run 和 DrawingAnalyzer.run。
入参：context: AnalyzerContext（系统上下文对象）。
出参：List[ArtifactRecord]（返回一个合并了的列表，里面既包含了 3D 引擎生成的 measurements.jsonl，也包含了 2D 引擎生成的 drawing_observations.jsonl）。

**健壮性优化：主辅容错，3D 分析是主线，如果 3D 解析崩溃，直接报错；2D 解析是辅助数据，如果 2D 图纸损毁报错，系统会默默吃掉 2D 的异常，降级返回 3D 的分析结果。**

### pyproject.toml更改：
为了遵循主项目的部署规范（`docs/dfm-deployment-environment.md`），2D 管线的依赖已并入主项目的可选依赖组 `[dfm]`。现在的 `pyproject.toml` 包含：
```toml
[project.optional-dependencies]
dfm = [
  "python-pptx==1.0.2",
  "rapidocr-onnxruntime>=1.4.0",
  "pymupdf>=1.20.0",
  "aspose-cad>=24.0.0"
]
```
部署时只需执行：`pip install -e ".[dfm]"`
