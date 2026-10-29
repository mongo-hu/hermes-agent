---
title: "DFM 部署环境定义"
status: implementation-reference
updated: 2026-10-29
type: deployment-guide
---

# DFM 部署环境定义

[开发导航](README.md) · [运行排查](dfm-analysis-runbook.md)

本文件只维护依赖、配置、启动与环境验证。总体流程在[主流程](dfm-main-workflow-explained.md)，常驻 Worker 和两轮确认的接入在[会话设计](dfm-geometric-session-contract.md)，不在此复制全流程图。

当前工作区的外部链路使用 dfm.geometry/v2 常驻 Worker，配套 Schema bundle 2.3.0；预览、两轮识别、人工修订和 G 计算复用模型资源。真实 Worker、独立安装目录及 Electron 两轮确认到异步 HTML 演示已联调，记录见会话文档第 10 节。自然语言聊天全场景与正式发布包仍单独验收；旧安装 EXE 不会因更新 Hermes 源码自动升级。PythonOCC 仅在选择内部参考后端时需要，不要求 C++ 引擎安装进 Agent 的 Python 环境。

`available` 表示依赖与能力探测成功，不等于算法已获工程认证。不要将参考结果或旧 Capability 直接当作生产发布依据。

## 1. 版本基线

### 1.1 Hermes 与 PythonOCC 参考环境

| 项目 | 要求 |
| --- | --- |
| Python | `>=3.11,<3.14` |
| PythonOCC | 与所用 OCCT ABI 匹配并锁定版本 |
| VTK | 壁厚参考采样需要 |
| PPTX | `python-pptx==1.0.2`，由 `dfm` extra 管理 |
| 编码 | UTF-8 |

### 1.2 OCCT C++ 项目（当前 experimental，目标 production）

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

### 1.3 分别记录三种版本

| 内容 | 核对位置 | 发布记录要求 |
| --- | --- | --- |
| 本体规则发布包 | 本地 SQLite 元数据、Plan 固定身份、ontology_snapshot.schema.json | schema_version、发布身份、规则集版本与哈希；不把仓库示例当作最新 |
| 几何通信 | EXE 能力、runtime/process.py 和 Worker schemas/session-v2.schema.json | contract_version=dfm.geometry/v2、schema_bundle_version、engine_version、dependency_fingerprint；与实际安装文件匹配 |
| 显示与测量制品 | 实际 Artifact Schema、Manifest 和内容哈希 | 几何／网格身份、采样精度和制品版本，不混用三角形编号 |

Agent 当前接受规则包 Schema 2/3/4，后续新规则使用 Schema 4；这是规则包版本，不等于几何通信 v2。新外部链路不提供旧 Worker 公共协议适配，不能只改版本号混用。

## 2. 当前 Python 依赖

DFM 的 Python 依赖在 `pyproject.toml` 的 `[project.optional-dependencies].dfm` 中声明，
并由 `uv.lock` 锁定。其中 HTML 报告编辑器需要 Python 包 `playwright`，还需要与该包匹配的
Playwright Chromium 浏览器。请在 Hermes Agent 仓库根目录、使用**启动 Agent 的同一个 Python 环境**执行：

```powershell
python -c "import sys; print(sys.executable)"
python -m pip install -e ".[dfm]"
python -m playwright install chromium
```

使用 uv 安装 Python 依赖时，对应命令是：

```powershell
uv sync --active --extra dfm --locked
python -m playwright install chromium
```

`pip install -e ".[dfm]"` 和 `uv sync --active --extra dfm --locked` 安装的是 Python 包；
二者都**不会**自动下载 Chromium。浏览器安装命令应在同一已激活环境中运行，
不能以系统 Chrome 或 Electron 自带浏览器代替。若只升级了 Playwright，也应重新检查浏览器。

当前 PythonOCC 参考 Worker 仍通过 conda-forge 安装原生库：

```powershell
conda install -n hermes-dev -c conda-forge pythonocc-core vtk
```

这组依赖不应被复制为 OCCT C++ 生产项目的构建方式。生产项目独立管理 OCCT C++、编译器、
链接库和镜像，并通过契约测试证明兼容。

## 3. Windows 开发环境

### 3.1 Hermes/Python 参考环境

```powershell
conda create -n hermes-dev -c conda-forge python=3.11 pythonocc-core vtk pip
conda activate hermes-dev
python -m pip install -e ".[dfm]"
python -m playwright install chromium
```

按路径创建的环境（例如 `D:\python-envs\hermes-agent`）应使用
`conda activate D:\python-envs\hermes-agent`，并确认 `python` 指向
`.vscode/launch.json` 中启动后端的解释器。独立终端还应使用与后端相同的
`HERMES_HOME`，否则 doctor 会读取另一份配置；例如本地调试配置使用
`$env:USERPROFILE\.hermes` 时，安装完成后在仓库根目录运行：

```powershell
$env:HERMES_HOME = Join-Path $env:USERPROFILE '.hermes'
python .\hermes dfm doctor
python .\hermes dfm doctor --json
```

检查文本输出中的 `HTML report Playwright installed`、`HTML report Chromium usable`
是否均为 `True`；JSON 输出检查 `html_report.ready` 是否为 `true`，并查看
`html_report.python_executable` 是否是启动 Agent 的环境。这个检查会短暂启动并关闭
Chromium，不会创建报告。`dfm doctor` 的顶层 `ok` 仅反映配置与工作区基础状态，
不能代替对 `html_report.ready` 的检查。

### 3.2 OCCT C++ 项目

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

### 3.3 Hermes 启动

```powershell
python .\hermes serve --host 127.0.0.1 --port 9120
npm run dev --workspace apps/desktop
```

## 4. DFM 配置

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
  ontology:
    endpoint: "http://<Mold后端地址>:8000"
    process: injection
    sync_interval_seconds: 300
    # organization_id: "<实际组织ID>"
    # pinned_snapshot_id: "<需要固定的发布身份>"
  geometry:
    backend: occt_cpp_external
    executable: "C:/path/to/dfm-geometry.exe"
    timeout_seconds: 900
    session_lease_seconds: 900
    session_idle_timeout_seconds: 1800
    session_max_processes: 2
    session_memory_budget_mb: 4096
    job_poll_interval_ms: 100
  evidence:
    max_markers_per_image: 10
  retention:
    keep_failed_runs: true
  drawing:
    enabled: true
```

`dfm.geometry.backend` 使用几何引擎实现和部署位置命名，不使用 STEP 输入格式作为后端名称：

- `occt_cpp_external`：调用独立的 C++ `dfm-geometry` 可执行程序；
- `pythonocc_internal`：调用 Agent 仓库内置的 PythonOCC 参考 Worker。

旧值 `occt_cpp`、`step` 仅作为兼容别名继续读取，并分别归一化为上述两个名称；新配置不要再使用旧值。
内部 Analyzer key 暂时仍为 `occt_cpp`、`step`，用于兼容已有 Plan、Run 和 Artifact，不属于用户配置契约。

`dfm.geometry.executable` 可留空；此时 Hermes 会检查仓库旁的标准 `dfm-geometry` 构建/安装
目录，再检查 `PATH`。配置相对路径时以 Hermes 仓库根目录解析。找不到程序时 OCCT Capability
显式返回依赖缺失，PythonOCC、NX/Parasolid 和其他占位能力仍按各自原有状态保留。

二维图纸管线只执行程序化 OCR，没有独立模型、Endpoint、API Key 或模型超时配置。OCR 后的语义
判断与 2D/3D 关联复用当前 Hermes 会话已经选择的大模型和凭据；Agent 提议 Observation/FusionLink，
DFM Runtime 校验契约并落库，几何模块验证 Feature/Region 与拓扑关系。其他行为设置进入
`config.yaml`，凭据仍按 Hermes 通用规则进入 secret 存储或 `.env`。NX 的遗留配置不是当前生产
路线，新的部署不得依赖它完成 STEP 分析。DWG 当前未纳入正式输入格式，也不安装 Aspose.CAD 等
商业 SDK。

## 5. 容器部署

当前 PythonOCC 参考镜像可以用于开发和契约回归，但不得标记为生产 DFM 镜像。部署形态需区分已接入与后续方案：

1. **同机/同容器独立进程**：当前接入方式。Hermes 管理 serve --stdio 进程与会话，不需要手工先启动 EXE；容器自身仍需验证匹配的运行库。
2. **独立计算服务**：后续部署选项；当前会话客户端不是 HTTP 客户端，不宣称已可直接配置 REST 地址使用。

无论哪种形态，都必须：

- 使用非 root 用户；
- 为输入、Job 和 Artifact 配置隔离目录与容量限制；
- 固定 Hermes、Engine、OCCT 和 Schema 版本；
- 配置 CPU、内存、临时磁盘、内部线程数、并发和超时；
- 不把 Token、客户路径或模型正文写入日志和镜像；
- Result 原子发布，Artifact 校验大小和 SHA256；
- Worker 崩溃不影响 Hermes 主进程和其它 Job。

## 6. 环境验证

### 6.1 当前参考环境

```powershell
python -c "from OCC.Core.BRep import BRep_Tool; print('OCC reference OK')"
python -c "import vtk, pptx; from PIL import Image; print('Reference dependencies OK')"
python .\hermes dfm doctor --json
```

`dfm doctor` 当前检查 Hermes 配置、工作区、PythonOCC 参考 Worker、`dfm-geometry` 可执行程序
及 Capability、ProcessAdapter 和随仓库默认 Snapshot；同时在 Agent Python 中检查 Playwright
是否已安装、Chromium 能否实际启动，并以 `html_report.ready` 报告 HTML 生成环境状态。
OCCT 状态为 available 只证明 experimental
Adapter 可执行，不代表 Django 发布、签名同步、生产认证或模具工程验收已经完成。

### 6.2 版本联调与发布验收

- 固定 Hermes 提交、Worker 构建与 OCCT／编译器 ABI、公共 Schema 和规则发布身份；使用对应正负契约样例，缺字段明确失败。
- 真实模型跑通识别、用户审核、所需 G、规则、问题与三维证据；available 不能代替精度、误报／漏报与工程验收。
- 验证大模型的时间／内存、取消、超时、崩溃与恢复；外部后端失败不自动降级内部 PythonOCC。
- HTML 在 Agent 同一解释器可渲染，报告任务独立排队，已有问题无需等待报告。

每次部署仍需验证：同一健康会话 STEP 只导入一次；两轮按组确认且就绪分支先算；续租／查询／取消通道可用；改局部方向只失效相关结果；Viewer 与同版报告使用一致证据；重启后旧句柄显式失效。已有源码专项证据见会话文档第 10 节，不把 doctor 的 available 当作这些行为已在当前安装环境全部验收。

### 6.3 本轮 Worker 安装验证

Windows 开发验证目录为 D:/workspace/occt-worker/out/install/session-2.3.0-verification-20261028。该目录的 bin/dfm-geometry.exe、运行库及 share/dfm-geometry/schemas 一起交付；不能只复制 EXE。已从其它工作目录、仅包含 Windows/System32 和 Windows 的 PATH 运行公共会话契约 9 项及 Hermes 解析几何联调 5 项，均通过。具体命令及完整构建脚本见 Worker 会话文档第 8.10 节。

此目录没有覆盖用户配置或 Hermes 随包 EXE。开发接入应将 geometry.executable 明确指向匹配构建，再用同一 profile 的 doctor 核对实际版本；正式发布需固定可追溯源码和完整运行库，不能把当前含未提交修改的 Desktop build 当作发布包。

## 7. 常见问题

| 现象 | 检查项 |
| --- | --- |
| `ModuleNotFoundError: OCC` | 当前运行的是 PythonOCC 参考链路，解释器是否安装对应 Conda 包 |
| C++ 找不到 OCCT DLL/so | 编译器 ABI、Debug/Release、运行库搜索路径和 OCCT 发布物是否匹配 |
| STEP 读取结果不一致 | Loader/Healing 参数、单位、OCCT 版本和输入 SHA256 |
| 几何引用失效 | 是否跨 TopologySnapshot 复用了 Face index，GeometrySnapshot 是否一致 |
| 并发时随机崩溃 | 是否共享 Shape/算法对象，内部线程是否过度订阅，算法是否可重入 |
| 结果有图但无法定位 | Scene、TopologyMap、ScalarField 是否来自同一 RenderMeshSnapshot |
| C++ 后端失败后仍有结果 | 检查是否发生了禁止的 PythonOCC 静默降级 |

## 8. 发布记录

保存实际安装版本、规则身份与哈希、环境 doctor 输出、契约测试结果和真实样件验收记录。明确未实现／不适用能力、非认证结果及部署限制；不要从目标规划推断支持范围。

会话配置名及部署方式随实际实现更新本文件，运行产物与故障诊断维护在运行手册。旧协议的设计背景见[历史 OCCT 方案](archive/2026-10-19/plans/2026-08-18-dfm-architecture-workflow-and-occt-contract.md)，不从历史方案推断当前安装版本。
