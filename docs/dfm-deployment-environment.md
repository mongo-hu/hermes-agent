---
title: "DFM 部署环境定义"
status: active
updated: 2026-08-25
type: deployment-guide
---

# DFM 部署环境定义

本文定义 Hermes DFM 能力当前可运行的实验环境和目标生产环境。Hermes 已通过本地 CLI 契约
接入独立 Analysis Situs/OCCT `dfm-geometry` 可执行文件；其当前 Capability 为
`experimental`，不等于生产认证。PythonOCC 与 NX 不在当前执行或降级链路中。

一次分析的数据和排障方式见 [单次 DFM 分析数据说明](dfm-analysis-runbook.md)，生产架构和
跨项目契约见 [DFM 架构、工作流与 OCCT C++ 契约](plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md)。

## 1. 能力状态

| 能力 | 当前实验环境 | 目标生产环境 |
| --- | --- | --- |
| STEP | 独立 Analysis Situs/OCCT Engine，实验级 | 同一外部 Engine 的认证发布物 |
| 特征识别 | Hermes ordinary whole-model fallback；外部 Engine 可产出 Objective features，但尚未接入两阶段 Discovery | OCCT C++ Recognizer + 可选 ML 候选增强 |
| 指标 | 外部 Engine 壁厚/拔模等 Objective Calculator，实验级 | 认证的 OCCT C++ 区域化 Calculator |
| 本体/规则 | 随仓库 Snapshot Schema 2、本地 SQLite、Hermes 确定性执行 | Django 发布系统/企业 Snapshot；Hermes 固定版本并执行 |
| 报告 | Hermes | Hermes，保持不变 |
| Parasolid/NX | intake 与执行路径均已移除，当前延期 | 未来可选 Backend，不阻塞 STEP |
| 2D/OCR/Fusion | 占位 | 后续里程碑 |

只有通过 `certified` Capability、正式 E2E 和工程验收的 OCCT C++ 版本才能被声明为生产可用。
当前 `available` 仅表示实验级 Engine 协议、版本和依赖探测通过，并且 Plan 已显式选择
`verification_level=experimental`。

## 2. 进程与服务架构

### 2.1 当前实验链路

```text
Desktop / CLI / API
        |
        v
Hermes Agent -> Discovery fallback -> Local Ontology SQLite + AnalysisPlan
                                                    |
                                                    v
                                         Job Manager -> dfm-geometry
                                                       |
                                                       +-- preflight / topology
                                                       +-- render_mesh / features
                                                       +-- measurements / metric_fields / engine_result
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

当前实现使用本地 CLI。后续远程服务必须保持相同的 Objective、Capability、WorkerEvent 和
Artifact 语义；Geometry Discovery Schema 1 已存在，但外部 Recognizer 调用尚未接入当前
`discover` 动作。

## 3. 版本基线

### 3.1 Hermes 运行环境

| 项目 | 要求 |
| --- | --- |
| Python | `>=3.11,<3.14` |
| PPTX | `python-pptx==1.0.2`，由 `dfm` extra 管理 |
| 编码 | UTF-8 |

### 3.2 Analysis Situs/OCCT C++ 项目

| 项目 | 要求 |
| --- | --- |
| C++ | C++17，项目内固定 |
| 构建 | CMake；推荐 Ninja 或平台原生生成器 |
| Windows | MSVC x64 与匹配的 Windows SDK |
| Linux | 受支持的 GCC/Clang x86_64 工具链 |
| Analysis Situs | `v2025.2`，提交 `aa5958932c8c85c068566ab685f2b99c0436b926` |
| OCCT | `7.9.3`；固定编译器 ABI、构建选项和第三方依赖 |
| 测试 | CTest + 单元/契约/Golden/E2E；按需使用 sanitizer |
| 交付 | 版本化可执行文件或容器镜像，不使用漂移的 `latest` |

`dfm-geometry` 是独立 C++ 项目/发布物。Hermes 不把其源代码或 Python 绑定编入核心；Analyzer
通过 `capabilities` 子命令校验 Engine、Analysis Situs、OCCT、操作表和成熟度，再通过
`analyze --request <request.json>` 执行。

### 3.3 当前协议和发布基线

| 契约 | 当前版本 |
| --- | --- |
| Ontology Snapshot | Schema 2；默认 `ontology.injection.default@1.1.0` |
| Geometry Discovery | Schema 1 |
| 外部 Objective Task | Schema 2；`dfm.geometry.request/v1` |
| 外部 WorkerEvent / Result | `dfm.geometry.event/v1` / `dfm.geometry.result/v1` |
| 外部 Artifact | preflight/topology/render-mesh/features/measurements/metric-fields v1 |
| Geometry Backend Capability | Schema 1 |

这些版本表示当前 Hermes Analyzer 实际校验的边界；Discovery Schema 1 和通用 Backend
Capability Schema 1 还包括下一阶段外部 Recognizer 的契约，不应误称为当前已执行的发现链路。

## 4. 当前 Python 依赖

报告等纯 Python 依赖由 `pyproject.toml` 与 `uv.lock` 管理：

```powershell
python -m pip install -e ".[dfm]"
```

或：

```powershell
uv sync --active --extra dfm --locked
```

Python 环境不安装 `pythonocc-core` 或 VTK 作为几何后端。C++ 项目独立管理 Analysis Situs、
OCCT、编译器和运行库；Hermes 只配置版本化可执行文件路径并通过契约探测兼容性。

## 5. Windows 开发环境

### 5.1 Hermes/Python 环境

```powershell
conda create -n hermes-dev python=3.11 pip
conda activate hermes-dev
python -m pip install -e ".[dfm]"
```

### 5.2 Analysis Situs/OCCT C++ 项目

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
  geometry:
    executable: dfm-geometry/out/install/windows-vcpkg-vs2026-sln/bin/dfm-geometry.exe
    timeout_seconds: 900
```

相对 `executable` 固定以当前 Hermes 源码根目录解析，不受桌面启动目录或项目目录
影响。若省略 `executable`，按仓内标准 install/build 目录及 PATH 顺序发现。不得为
这些设置新增 `.env` 变量。

运行：

```powershell
hermes dfm doctor --json
```

`dfm doctor` 当前检查 Hermes 配置、工作区、外部 `dfm-geometry` 可执行文件与 Capability、
ProcessAdapter 和随仓库默认 Snapshot 能否编译。它不验证 Django 签名同步、撤销列表，也不
代表实验级算法已经获得生产认证。

### 8.2 OCCT C++ 生产验收

1. `GET /v1/capabilities` 或本地等价命令返回正式 Capability Schema 1；
2. Geometry Discovery Schema 1 的正负 Fixture 全部通过；
3. 当前 Objective Schema 2 与 request/event/result v1 的正负 Fixture 全部通过；升级契约时双方必须原子发布；
4. STEP Loader、Snapshot、首批 Recognizer 和 Calculator 均为 `certified`；
5. 真实产品完成 Discovery → Plan → Objective → Report E2E；
6. 并发、超时、取消、崩溃、资源限制和 Artifact 恢复通过测试；
7. Engine 缺失、不健康或认证级别不足时显式失败，PythonOCC/NX 不会被自动调用。

## 9. 常见问题

| 现象 | 检查项 |
| --- | --- |
| `geometry_engine_missing` | `dfm.geometry.executable`、标准 build/install 目录或 PATH 中是否存在 `dfm-geometry` |
| `geometry_protocol_invalid` | Engine/Analysis Situs/OCCT 版本、操作表、JSON/JSONL 字段和协议版本是否与 Hermes 固定契约一致 |
| C++ 找不到 OCCT DLL/so | 编译器 ABI、Debug/Release、运行库搜索路径和 OCCT 发布物是否匹配 |
| STEP 读取结果不一致 | Loader/Healing 参数、单位、OCCT 版本和输入 SHA256 |
| Region 引用失效 | 是否跨 TopologySnapshot 复用了 Face index，GeometrySnapshot 是否一致 |
| 并发时随机崩溃 | 是否共享 Shape/算法对象，内部线程是否过度订阅，算法是否可重入 |
| 结果有图但无法定位 | Scene、TopologyMap、ScalarField 是否来自同一 RenderMeshSnapshot |
| C++ 后端失败后仍有结果 | 只接受同一 Run 已登记且哈希通过的制品；检查是否误读旧 Run，任何后端静默降级都属于缺陷 |

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
