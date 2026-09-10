---
title: "DFM 部署环境定义"
status: active
updated: 2026-08-25
type: deployment-guide
---

# DFM 部署环境定义

本文定义 Hermes DFM 能力的当前参考环境和目标生产环境。PythonOCC Worker 继续用于开发、
演示和契约回归；独立 OCCT C++ `dfm-geometry` 可执行程序已经以 experimental Analyzer 接入，
但生产级特征识别与指标计算仍需 certified 工程验收。两者不要求安装在同一个 Python 环境。

一次分析的数据和排障方式见 [单次 DFM 分析数据说明](dfm-analysis-runbook.md)，生产架构和
跨项目契约见 [DFM 架构、工作流与 OCCT C++ 契约](plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md)。

## 1. 能力状态

| 能力 | 当前参考环境 | 目标生产环境 |
| --- | --- | --- |
| STEP | PythonOCC 可运行、非认证；`dfm-geometry` experimental 可执行程序已接入 | certified 独立 OCCT C++ Engine |
| 特征识别 | ordinary whole-model Discovery fallback；C++ Recognizer 产物为 experimental | OCCT C++ Recognizer + 可选 ML 候选增强 |
| 指标 | PythonOCC 壁厚/拔模角参考场；C++ Calculator 为 experimental | certified OCCT C++ 区域化 Calculator |
| 本体/规则 | 随仓库 Snapshot Schema 2、本地 SQLite、Hermes 确定性执行 | Django 发布系统/企业 Snapshot；Hermes 固定版本并执行 |
| 报告 | Hermes | Hermes，保持不变 |
| Desktop 3D | Three.js 查看器消费 `hermes.dfm.viewer/v2` Manifest，直接读取共享 RenderScene/TopologyMap | 同一 Manifest 契约，随生产 Artifact 验收 |
| Parasolid/NX | 登记或遗留边界，当前延期 | 未来可选 Backend，不阻塞 STEP |
| 2D/OCR/Fusion | PDF/图片程序化 OCR；Hermes Agent 提议，程序落库，几何验证的基础闭环 | 复杂标注与空间融合后续验收 |

只有通过 `certified` Capability、正式 E2E 和工程验收的 OCCT C++ 版本才能被声明为生产可用。
PythonOCC 的 `available` 只表示参考 Worker 依赖齐全。

## 2. 进程与服务架构

### 2.1 当前参考链路

```text
Desktop / CLI / API
        |
        v
Hermes Agent -> DFM Service -> Local Ontology SQLite + AnalysisPlan
                                |
                                v
                           Job Manager -+-> PythonOCC reference worker
                                        +-> dfm-geometry experimental worker
                                             |
                                             +-- measurements / field / scene / map / viewer
```

### 2.2 目标生产链路

```text
Desktop / CLI / API
        |
        v
Published Snapshot -> Agent Local SQLite
                           |
Hermes Agent -> DFM Service -> Geometry Backend Adapter
                                      |
                                      v
                              OCCT C++ Engine
                         discovery jobs / objective jobs
                                      |
                                      v
                         immutable artifacts + manifests
```

独立 C++ 项目可以先交付本地 CLI Worker，后续封装为远程服务。两种方式消费相同的
Geometry Discovery、Objective、Capability、WorkerEvent 和 Artifact 契约。

## 3. 版本基线

### 3.1 Hermes 与 PythonOCC 参考环境

| 项目 | 要求 |
| --- | --- |
| Python | `>=3.11,<3.14` |
| PythonOCC | 与所用 OCCT ABI 匹配并锁定版本 |
| VTK | 壁厚参考采样需要 |
| PPTX | `python-pptx==1.0.2`，由 `dfm` extra 管理 |
| 编码 | UTF-8 |

### 3.2 OCCT C++ 项目（当前 experimental，目标 production）

| 项目 | 要求 |
| --- | --- |
| C++ | C++17 或 C++20，项目内固定 |
| 构建 | CMake；推荐 Ninja 或平台原生生成器 |
| Windows | MSVC x64 与匹配的 Windows SDK |
| Linux | 受支持的 GCC/Clang x86_64 工具链 |
| OCCT | 固定明确版本、编译器 ABI、构建选项和第三方依赖 |
| 测试 | CTest + 单元/契约/Golden/E2E；按需使用 sanitizer |
| 交付 | 版本化可执行文件或容器镜像，不使用漂移的 `latest` |

`dfm-geometry` 作为独立 C++ 项目构建；其本地源码/构建目录被 Hermes `.gitignore` 排除。
Hermes 不链接或 vendoring C++ 内部库，只发现已构建的可执行程序，并依赖其 Capability 与
版本化进程协议。

### 3.3 当前协议和发布基线

| 契约 | 当前版本 |
| --- | --- |
| Ontology Snapshot | Schema 2；默认 `ontology.injection.default@1.2.0` |
| Geometry Discovery | Schema 1 |
| Objective | Schema 4 |
| 本地 Geometry 进程边界 | `dfm.geometry.request/v1`、`dfm.geometry.event/v1`、`dfm.geometry.result/v1`；信封内 Objective Task/Result 均为 Schema 4 |
| ScalarField/RenderScene/TopologyMap/Evidence | Schema 2 |
| Geometry Backend Capability | Schema 1 |

这些版本表示 Hermes 侧已经存在的契约基线，不表示 `dfm-occt-worker` 或 Django 管理服务已经部署。

## 4. 当前 Python 依赖

报告等纯 Python 依赖由 `pyproject.toml` 与 `uv.lock` 管理：

```powershell
python -m pip install -e ".[dfm]"
```

或：

```powershell
uv sync --active --extra dfm --locked
```

当前 PythonOCC 参考 Worker 仍通过 conda-forge 安装原生库：

```powershell
conda install -n hermes-dev -c conda-forge pythonocc-core vtk
```

这组依赖不应被复制为 OCCT C++ 生产项目的构建方式。生产项目独立管理 OCCT C++、编译器、
链接库和镜像，并通过契约测试证明兼容。

## 5. Windows 开发环境

### 5.1 Hermes/Python 参考环境

```powershell
conda create -n hermes-dev -c conda-forge python=3.11 pythonocc-core vtk pip
conda activate hermes-dev
python -m pip install -e ".[dfm]"
```

### 5.2 OCCT C++ 项目

建议安装：

- Visual Studio Build Tools 的 Desktop development with C++；
- CMake、Ninja；
- VS Code C/C++ 与 CMake Tools；
- 与 MSVC ABI 匹配的 OCCT 构建或二进制发布物。

`dfm-geometry` 独立项目典型命令：

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build
ctest --test-dir build --output-on-failure
```

实际 OCCT 路径、Toolchain 和 Preset 由 C++ 项目维护，不写死在 Hermes 配置中。

### 5.3 Hermes 启动

```powershell
python .\hermes serve --host 127.0.0.1 --port 9120
npm run dev --workspace apps/desktop
```

## 6. DFM 配置

当前代码可使用：

```yaml
dfm:
  runtime:
    python: auto
    max_concurrent_runs: 1
    timeout_seconds: 900
  intake:
    max_file_size_mb: 200
    max_pages: 50
  defaults:
    process: injection
  geometry:
    executable: "C:/path/to/dfm-geometry.exe"
    timeout_seconds: 900
  evidence:
    max_rendered_findings: 12
  retention:
    keep_failed_runs: true
  geometry:
    backend: step       # 当前参考实现；生产 OCCT C++ Adapter 注册后改为其 analyzer key
  drawing:
    enabled: true
```

`dfm.geometry.executable` 可留空；此时 Hermes 会检查仓库旁的标准 `dfm-geometry` 构建/安装
目录，再检查 `PATH`。配置相对路径时以 Hermes 仓库根目录解析。找不到程序时 OCCT Capability
显式返回依赖缺失，PythonOCC、NX/Parasolid 和其他占位能力仍按各自原有状态保留。

二维图纸管线只执行程序化 OCR，没有独立模型、Endpoint、API Key 或模型超时配置。OCR 后的语义
判断与 2D/3D 关联复用当前 Hermes 会话已经选择的大模型和凭据；Agent 提议 Observation/FusionLink，
DFM Runtime 校验契约并落库，几何模块验证 Feature/Region 与拓扑关系。其他行为设置进入
`config.yaml`，凭据仍按 Hermes 通用规则进入 secret 存储或 `.env`。NX 的遗留配置不是当前生产
路线，新的部署不得依赖它完成 STEP 分析。DWG 当前未纳入正式输入格式，也不安装 Aspose.CAD 等
商业 SDK。

## 7. 容器部署

当前 PythonOCC 参考镜像可以用于开发和契约回归，但不得标记为生产 DFM 镜像。目标部署有
两种受支持形态：

1. **同容器独立进程**：Hermes 调用已安装的 OCCT C++ Worker；适合单机和早期 E2E；
2. **独立计算服务**：Hermes 通过受控 HTTP Adapter 提交 Job；适合水平扩展和资源隔离。

无论哪种形态，都必须：

- 使用非 root 用户；
- 为输入、Job 和 Artifact 配置隔离目录与容量限制；
- 固定 Hermes、Engine、OCCT 和 Schema 版本；
- 配置 CPU、内存、临时磁盘、内部线程数、并发和超时；
- 不把 Token、客户路径或模型正文写入日志和镜像；
- Result 原子发布，Artifact 校验大小和 SHA256；
- Worker 崩溃不影响 Hermes 主进程和其它 Job。

## 8. 环境验证

### 8.1 当前参考环境

```powershell
python -c "from OCC.Core.BRep import BRep_Tool; print('OCC reference OK')"
python -c "import vtk, pptx; from PIL import Image; print('Reference dependencies OK')"
python .\hermes dfm doctor --json
```

`dfm doctor` 当前检查 Hermes 配置、工作区、PythonOCC 参考 Worker、`dfm-geometry` 可执行程序
及 Capability、ProcessAdapter 和随仓库默认 Snapshot。OCCT 状态为 available 只证明 experimental
Adapter 可执行，不代表 Django 发布、签名同步、生产认证或模具工程验收已经完成。

### 8.2 OCCT C++ 生产验收

1. `GET /v1/capabilities` 或本地等价命令返回正式 Capability Schema 1；
2. Geometry Discovery Schema 1 的正负 Fixture 全部通过；
3. Objective Schema 4 与 Geometry/Evidence Schema 2 的正负 Fixture 全部通过；
4. STEP Loader、Snapshot、首批 Recognizer 和 Calculator 均为 `certified`；
5. 真实产品完成 Discovery → Plan → Objective → Report E2E；
6. 并发、超时、取消、崩溃、资源限制和 Artifact 恢复通过测试；
7. PythonOCC reference 不会在生产失败时被自动调用。

## 9. 常见问题

| 现象 | 检查项 |
| --- | --- |
| `ModuleNotFoundError: OCC` | 当前运行的是 PythonOCC 参考链路，解释器是否安装对应 Conda 包 |
| C++ 找不到 OCCT DLL/so | 编译器 ABI、Debug/Release、运行库搜索路径和 OCCT 发布物是否匹配 |
| STEP 读取结果不一致 | Loader/Healing 参数、单位、OCCT 版本和输入 SHA256 |
| Region 引用失效 | 是否跨 TopologySnapshot 复用了 Face index，GeometrySnapshot 是否一致 |
| 并发时随机崩溃 | 是否共享 Shape/算法对象，内部线程是否过度订阅，算法是否可重入 |
| 结果有图但无法定位 | Scene、TopologyMap、ScalarField 是否来自同一 RenderMeshSnapshot |
| C++ 后端失败后仍有结果 | 检查是否发生了禁止的 PythonOCC 静默降级 |

## 10. 发布前检查清单

- Hermes 与 OCCT C++ 项目引用相同 Schema 发布版本；
- Agent 固定的 Ontology Snapshot Schema 2、ID 和 SHA256 可追溯，企业发布物签名与撤销状态已校验；
- 每个 Check 的 Operand 能通过 `APPLIES_TO_REGION/HAS_REGION/APPLIES_TO_FEATURE` 唯一解析，并与
  Worker Capability 中的 Metric/Quantity 对齐；
- C++ 编译器、OCCT、第三方库和镜像 Digest 已固定；
- 每个生产 Recognizer/Calculator 均有认证报告哈希；
- 合成真值、真实脱敏产品和对抗模型均通过；
- Feature/Region 覆盖、重叠、误报和漏报已经工程验收；
- 指标数值、控制极值、局部场和证据定位已经工程验收；
- Windows 开发和 Linux 生产环境均通过契约与 E2E；
- 大模型、并发、取消、超时、崩溃和长期运行已经验证；
- 未实现能力显式返回，不误报为可用；
- NX/Parasolid 延期不会阻塞 STEP 生产发布。
