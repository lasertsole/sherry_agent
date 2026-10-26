# 计划:统一打包 + 可选 CUDA 懒下载(无环境变量依赖)

## 设计概览

把当前"小安装包 + 首启下载后端"的双产物模式,改成 **Electron 式统一打包 + 可选 CUDA 懒下载**:

1. **默认 CPU 版统一打包**:前端 SPA + CPU 版后端树(PyInstaller onedir)塞进同一个安装包,首启零下载。
2. **CUDA 可选懒下载**:安装后检测宿主机是否支持 CUDA;支持则在 **菜单 → 系统设置 → 模型设置 tab** 提供下载按钮,用户自主决策是否拉取 CUDA pack。不支持则不显示按钮。
3. **下载体验**:进度条 + 断点续传(HTTP Range),复用现有 `backend_downloader` 的 Tauri 事件机制 + `model_downloads` 的 WS push 基建。
4. **去除环境变量依赖**:后端启动不再通过 `SHERRY_HOME`/`SHERRY_DESKTOP` 环境变量传参,改 CLI 参数;CUDA 库加载不走 `LD_LIBRARY_PATH`,改 OS 动态加载 API。删除相关 env var 代码。

## 已确认的事实(前期实测)

| # | 事实 | 证据 |
|---|------|------|
| 1 | Windows 后端 zip 364 MB(压缩)/ 967 MB(解压)/ 9217 文件,远低于 GitHub 2 GiB 上限 | `unzip -l` 实测 |
| 2 | Windows 纯 CPU,无 CUDA 库(`torch/lib/torch_cpu.dll` 306 MB,无 `torch_cuda.dll`/`nvidia-*`) | zip 内目录 |
| 3 | 锁文件 `uv.lock` 里 torch 的 CUDA 依赖**全部** `marker = "sys_platform == 'linux'"` —— Windows/macOS 天然 CPU,Linux 是唯一需要"CPU 替换 + CUDA 可选"的平台 | `grep "marker" uv.lock` |
| 4 | Linux CI 已有 CPU torch swap(`desktop-build.yml:55-62`),产物 ~1 GiB,塞包可行 | CI 配置 |
| 5 | Python 侧 `ROOT_DIR = Path(__file__).parent / ".."` 自动跟随 exe 位置 — Python 端零改动 | `config/path.py:11-13` |
| 6 | `lib.rs` 已区分 `app_home`(可写 SHERRY_HOME)和后端 exe 位置 — 塞包只改 exe 指向 | `client/src-tauri/src/lib.rs` |
| 7 | 现有 `backend_downloader.rs` 已实现:进度事件(`backend-download-started/-progress/-complete/-error`)+ staging+swap + 版本绑定 + `EMA_BACKEND_URL_BASE` 覆盖 | `client/src-tauri/src/services/backend_downloader.rs` |
| 8 | 现有 `model_downloads_service.py` + `model_download_events.py` 已实现:per-family 后台 worker + `model_download_updated` WS push(节流 0.25s)+ 进度快照拉取 | `server/service/` |
| 9 | `python_process.rs:111,115` 当前设 `SHERRY_HOME` + `SHERRY_DESKTOP` 两个 env var;Python 侧 `config/path.py:22` 用 `os.environ.get("SHERRY_HOME")` 读 | grep 实测 |
| 10 | 模型设置面板已存在(模型配置 tab),已有模型下载进度条基建可复用 | AGENTS.md "模型配置 panel" |

## 任务分解

### A. 统一打包(默认 CPU 版)

#### A1. `tauri.conf.json` 加 `bundle.resources` ✅ 已改

```jsonc
"bundle": {
  "active": true,
  "targets": "all",
  "icon": [...],
  "resources": ["sherry-backend/"]
}
```

路径相对 `src-tauri/`,指向 `build_backend.py` 产出的 `sherry-backend/`。

#### A2. `lib.rs` 运行时指向 `resource_dir()` ✅ 已改

packaged 模式优先用 `resource_dir()/sherry-backend/sherry-backend.exe`;缺失才回退下载器(过渡期保留)。`SHERRY_HOME` 保持 `app_data_dir()` 不变(见 D 节进一步改为 CLI 传参)。

#### A3. CI 构建顺序

`desktop-build.yml` 的 `build-tauri` job 改为 `needs: build-backend` + 下载对应平台 backend 产物解压到 `client/src-tauri/sherry-backend/` 后再 `pnpm tauri build`。Linux 沿用现有 CPU torch swap。

#### A4. 三平台 backend 产物统一为 CPU 版

- Windows/macOS:torch wheel 天然 CPU,无需改动
- Linux:CI 已 swap CPU torch(`desktop-build.yml:55-62`),保持
- 产物:`sherry-backend-<os>-<arch>.tar.gz|.zip`(CPU 版),作为 release 资产(供下载器 fallback 用,以及给老版本用户)

### B. CUDA 检测 + 可选下载

#### B1. 宿主机 CUDA 支持检测

**新文件**:`client/src-tauri/src/services/cuda_detect.rs`

检测逻辑(三平台):
- **Windows**:跑 `nvidia-smi`(或在 `%ProgramFiles%\NVIDIA Corporation` 找 NVSMI);读驱动版本;可选检测 `nvcc`
- **Linux**:`nvidia-smi` 或 `/proc/driver/nvidia/version` 或 `lspci | grep -i nvidia`
- **macOS**:NVIDIA GPU 已废弃,直接返回不支持

返回结构:
```rust
pub struct CudaSupport {
    pub supported: bool,        // 有 NVIDIA GPU + 驱动
    pub driver_version: Option<String>,
    pub gpu_name: Option<String>,
    pub reason: Option<String>, // 不支持时给原因(无 GPU / 驱动太旧 / 平台不支持)
}
```

通过 Tauri command `cuda_support` 暴露给前端,首次打开模型设置 tab 时调用。

#### B2. CUDA pack 作为独立 release 资产

**新 release 资产**:`sherry-cuda-pack-<os>-<arch>.tar.gz|.zip`(Linux 专属,Windows/macOS 不产)

内容:从 CPU 版 backend 树里**剥出来**的 nvidia-* `.so` + triton + `libtorch_cuda.so`(CUDA 版 torch 的 CUDA 钩子二进制)。具体:

- **build 侧**(`packaging/build_cuda_pack.py` 新文件):用 CUDA 版 torch 跑一次 PyInstaller,然后从 `_internal/` 里把 `nvidia/*/lib/*.so`、`triton/`、`torch/lib/libtorch_cuda.so` 单独打包成 cuda-pack。CPU 版 backend 树保持不含这些。
- **体积**:~4.5 GiB(Linux),作为单独 release 资产(同 backend 一样超 2 GiB 的话分卷,但实测 CUDA 库压缩后 ~1.5-2 GiB,应该刚好卡线;若超限则拆 cuDNN/cublas/nccl 三个分卷)

**关键约束**:cuda-pack 的 torch 版本必须与 CPU 版 backend 里的 torch 版本严格一致(同一次 uv.lock 解析),否则 `libtorch_cuda.so` 与 CPU 版 `libtorch_cpu.so` ABI 不匹配。VERSION 文件携带 torch 版本号做校验。

#### B3. `cuda_pack_downloader.rs`(复用 backend_downloader 架构)

**新文件**:镜像 `backend_downloader.rs` 的结构:
- `CudaPackDownloader::new(app_home)` — pack 落位 `<app_home>/sherry-cuda-pack/`
- `pack_ready()` — 检测 pack 是否存在且 VERSION 匹配
- `download()` — 流式下载 + 进度事件(`cuda-pack-download-started/-progress/-complete/-error`)
- **新增断点续传**(见 C 节)
- `cuda_lib_dir()` — 返回 pack 解压后的 lib 目录,供 D 节动态加载用

#### B4. lib.rs 集成

packaged 模式启动后端前:
1. 调 `cuda_detect::detect()` 看宿主机支不支持
2. 不支持 → 直接 spawn CPU 版 backend(无 CUDA)
3. 支持 + pack 已下载 → spawn 时把 `cuda_lib_dir()` 通过动态加载 API 注入(见 D2)
4. 支持 + pack 未下载 → spawn CPU 版 backend;前端按钮可触发下载,下完需**重启后端**生效

### C. 进度条 + 断点续传

#### C1. 前端进度条

复用现有 `model_download_events.py` 的 `model_download_updated` WS push 机制 + `BackendDownloadOverlay.vue` 的渲染模式:

- cuda-pack 下载走 Tauri 事件(同 backend_downloader,因为发生在 shell 层,backend 还没起)
- 事件名:`cuda-pack-download-started/-progress/-complete/-error`
- 前端监听:`client/app/composables/use-cuda-pack-download.ts`(新文件,镜像 `use-backend-download.ts`)
- UI:模型设置 tab 内一个下载卡片(见 E 节),显示进度百分比 + 已下载/总大小 + 取消按钮

#### C2. HTTP Range 断点续传

`cuda_pack_downloader.rs::download()` 改造:

1. 下载前检查 `<app_home>/sherry-cuda-pack-download.tmp` 是否存在,读其大小 `existing`
2. 若 `existing > 0`,发请求带 `Range: bytes=existing-` 头
3. 服务端若支持 `Accept-Ranges: bytes`(返回 206),追加写入 tmp 文件
4. 服务端若不支持(返回 200),从头下,覆盖 tmp
5. 下载完成 → 校验 Content-Length 或 SHA → 重命名 tmp 为最终文件 → 解压到 staging → swap

**边界**:
- tmp 文件损坏(校验失败)→ 删 tmp 从头下
- 服务端 416(Requested Range Not Satisfiable,说明 tmp 比远程文件大)→ 删 tmp 从头下
- 中途取消 → 保留 tmp,下次继续

#### C3. 进度事件节流

复用 `backend_downloader.rs:61` 的 `PROGRESS_MIN_INTERVAL = 200ms` 节流,避免 64 KiB chunk 触发几千次事件。

### D. 去除环境变量依赖

#### D1. `SHERRY_HOME` 改 CLI 参数传递

**当前**(`python_process.rs:111`):
```rust
.env("SHERRY_HOME", &self.app_home)
.env("SHERRY_DESKTOP", "1")
```

**改为**:通过 CLI 参数传给后端:
```rust
.arg("--sherry-home").arg(&self.app_home)
.arg("--sherry-desktop")
```

**Python 侧改动**(`config/path.py:22` + 启动入口 `server/__main__.py` / `packaging/backend_entry.py`):
- 用 `argparse` 解析 `--sherry-home`、`--sherry-desktop`
- `config/path.py` 不再读 `os.environ.get("SHERRY_HOME")`,改读全局启动参数(或在 `__main__` 里 set 到一个 config 对象,`path.py` 从该对象读)
- 保留 env var 作为**仅 fallback**(向后兼容老版本/checkout 开发模式),但 packaged 模式不再设

**影响面**:全局搜 `os.environ.get("SHERRY_HOME")` / `SHERRY_DESKTOP` 的所有读取点,改成读 config 对象。AGENTS.md 提到 `SHERRY_DESKTOP=1` 由 shell 设置 → 改成 CLI flag。

#### D2. CUDA 库加载不走 `LD_LIBRARY_PATH`

**Linux 方案**:用 `dlopen` + 绝对路径,或更简单 —— 用 torch 自己的 `torch.ops.load_library()` / `torch.cuda` 初始化前注入:

```rust
// Rust 侧 spawn 前,若 cuda pack 已下载:
let cuda_lib = app_home.join("sherry-cuda-pack/lib");
// Linux:用 ld-linux 的 --library-path 参数(不是 env var,是动态链接器参数)
.arg(format!("/lib64/ld-linux-x86-64.so.2"))
.arg("--library-path")
.arg(cuda_lib)
.arg("--")
.arg(&backend_exe)
```
即通过 `ld-linux --library-path <dir> -- <exe>` 显式指定库搜索路径,**不污染环境变量**。这比 `LD_LIBRARY_PATH` env var 更内联,且只对该进程生效。

**Windows 方案**:用 `SetDllDirectoryW` / `AddDllDirectory` Win32 API(在 spawn 前 Rust 侧调用),或 `LoadLibraryExW` 带 `LOAD_LIBRARY_SEARCH_USER_DIRS`。Windows 暂不需要 CUDA pack(锁文件里 nvidia-* 全是 Linux-only 标记),但 API 预留:

```rust
#[cfg(windows)]
{
    use windows::Win32::System::LibraryLoader::SetDllDirectoryW;
    let _ = SetDllDirectoryW(&cuda_lib);  // 只影响当前进程的 DLL 搜索
}
```

**macOS**:无 NVIDIA GPU,跳过。

#### D3. 删除 env var 相关代码

清单:
- `python_process.rs:111,115` 的 `.env("SHERRY_HOME", ...)` / `.env("SHERRY_DESKTOP", ...)`
- `backend_downloader.rs` 里若设过 `EMA_BACKEND_URL_BASE` 之外的 env var,清理
- Python 侧 `os.environ.get("SHERRY_HOME")` / `os.environ.get("SHERRY_DESKTOP")` 读取点,改 config 对象
- AGENTS.md 文档同步更新

### E. 前端:模型设置 tab 的下载按钮

#### E1. UI 位置

菜单 → 系统设置 → 模型设置 tab(即现有"模型配置"面板,`client/app/pages/home/components/` 下)。

新增一个区块:**CUDA 加速(可选)**

```
┌─────────────────────────────────────────────┐
│ CUDA 加速(可选)                            │
│                                             │
│ 状态:● 检测中... / ✓ 支持(NVIDIA RTX 4090, 驱动 545.x) / ✗ 不支持(无 NVIDIA GPU) │
│                                             │
│ [下载 CUDA Pack]  已下载: 1.8 GiB / 4.5 GiB │
│ ████████████░░░░░░░░░  40%                  │
│                                             │
│ [取消] [安装并重启后端]                      │
│                                             │
│ 说明:CUDA pack 让本地语音识别和文档 VL 解析走 GPU,约快 3-5x。CPU 版功能完整,不下载也能用。 │
└─────────────────────────────────────────────┘
```

#### E2. 前端逻辑(`use-cuda-pack.ts` 新文件)

- `cudaSupport = ref<CudaSupport | null>(null)`
- onMounted 调 `invoke('cuda_support')` 检测
- 检测到 `supported=true` 才显示下载按钮;`false` 显示原因
- 下载按钮 → `invoke('download_cuda_pack')` → 监听 Tauri 事件更新进度
- 下完显示"安装并重启后端"按钮 → `invoke('restart_backend')`
- 已下载显示"卸载 CUDA pack"按钮 → `invoke('remove_cuda_pack')`

#### E3. i18n

四语言文案(EN/ZH/JA/KO),复用现有 `chatInput.*` / `model.*` 命名空间,新加 `cuda.*` 命名空间。

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | cuda-pack 的 torch 版本必须与 CPU 版 backend 的 torch ABI 严格匹配 | 高 | VERSION 文件带 torch 版本号;不匹配拒绝加载,提示重下 |
| R2 | `ld-linux --library-path` 方案在某些发行版路径不同(如 `/lib/ld-linux-aarch64.so.2`) | 中 | 运行时探测 `ldd --version` 或读 `/proc/self/auxv`,动态选 ld 路径 |
| R3 | cuda-pack 体积可能超 2 GiB GitHub 资产上限 | 中 | 实测压缩后 ~1.5-2 GiB 应卡线;若超限拆 cuDNN/cublas/nccl 三个分卷,下载器顺序拉 |
| R4 | 断点续传服务端不支持 Range | 低 | 检测 `Accept-Ranges` 头;不支持则从头下,提示"该源不支持续传" |
| R5 | 后端启动后改 CLI 参数,老 checkpoint 兼容 | 中 | `--sherry-home` 缺失时 fallback 读 env var(过渡期),下个大版本删 fallback |
| R6 | CUDA pack 下载完需重启后端(torch 在 import 时解析库路径) | 中 | UI 明确提示"安装并重启后端生效";`invoke('restart_backend')` 杀进程重启 |
| R7 | `bundle.resources` 大目录拖慢 tauri-build 复制 | 低 | 967 MB / 9217 文件,`std::fs::copy` 顺序拷贝 1-2 min,可接受 |
| R8 | macOS 路径验证(`resource_dir()` 与 `.app/Contents/Resources/` 落位) | 高 | 首启加 `tracing::info!(resource_dir = ...)` 日志;CI 产物里实测 |
| R9 | 三平台统一打包,Linux AppImage 体积 ~1.2 GiB | 低 | AppImage 无硬上限;deb/rpm 也 OK |

## 不在本次范围

- **Tauri updater 集成** — 当前没用 updater,本计划不引入;更新靠覆盖安装
- **删除 backend_downloader 代码** — 过渡期保留作 fallback(老版本用户、dev 构建、手动解包);塞包模式稳定 1-2 个 release 后单独 PR 删
- **Windows/macOS CUDA pack** — 锁文件里 nvidia-* 全是 Linux-only,Windows/macOS 的 torch wheel 天然无 CUDA;不产 Windows/macOS cuda-pack

## 落地顺序

### 阶段 1:统一打包(CPU 默认)— 可独立发布

1. A1 ✅ + A2 ✅(已改)
2. A3 CI 构建顺序
3. A4 三平台 CPU 产物确认
4. D1 + D3 env var 改 CLI 参数(可单独 PR,与打包解耦)
5. 本机/CI 实测 `pnpm tauri build` 产出含后端的 installer
6. 验证 installer 体积 ~400 MB(Windows)、首启日志见 `"Using the bundled Python backend"`

### 阶段 2:CUDA 检测 + 可选下载

7. B1 `cuda_detect.rs`
8. B2 `packaging/build_cuda_pack.py` + CI 产 cuda-pack release 资产
9. B3 + C2 `cuda_pack_downloader.rs`(含断点续传)
10. B4 `lib.rs` 集成检测 + 启动分支
11. D2 CUDA 库动态加载(`ld-linux --library-path`)
12. E1 + E2 + E3 前端模型设置 tab 的下载 UI

### 阶段 3:清理

13. 删 `backend_downloader.rs`(塞包稳定后)
14. 删 env var fallback(D1 的过渡期结束)
15. 文档同步(AGENTS.md、wiki)

## 测试清单

### 阶段 1 测试

- [ ] `pnpm tauri build` 产出 NSIS installer(Windows)
- [ ] installer 体积 ~400 MB(含后端)
- [ ] 静默安装到干净目录,首启**不触发下载**
- [ ] 日志见 `"Using the bundled Python backend (no download)"`
- [ ] 跑一次对话,后端正常工作
- [ ] `--sherry-home` CLI 参数被后端正确解析
- [ ] macOS `.app` 的 `resource_dir()` 路径正确
- [ ] Linux AppImage 体积 ~1.2 GiB,后端能 spawn

### 阶段 2 测试

- [ ] 无 NVIDIA GPU 的机器,模型设置 tab 不显示下载按钮
- [ ] 有 NVIDIA GPU 的机器,显示"支持"+ GPU 名 + 驱动版本
- [ ] 下载 cuda-pack,进度条实时更新
- [ ] 中途断网,重启后续传(从已下载字节继续)
- [ ] 下完"安装并重启后端"按钮,点击后 backend 重启
- [ ] 重启后 `torch.cuda.is_available()` 返回 true(后端日志验证)
- [ ] mineru VL 解析走 GPU(可通过耗时对比验证)
- [ ] 卸载 cuda-pack,后端回退 CPU,功能正常
- [ ] cuda-pack VERSION 与 backend torch 版本不匹配时,拒绝加载并提示

## 本次实测记录(2026-10-26)

**已验证**:
- Windows zip 364 MB / 967 MB 解压 / 9217 文件,< 2 GiB ✓
- Windows 纯 CPU torch(无 CUDA 库)✓
- VERSION=0.1.4 与发布安装包一致 ✓
- Python ROOT_DIR 自动跟随 exe 位置 ✓
- lib.rs 已区分 app_home 与 exe 位置 ✓
- T1(tauri.conf.json `bundle.resources`)+ T2(lib.rs `resource_dir()` 分支)代码已改 ✓
- 真实后端树已提取到 `client/src-tauri/sherry-backend/`(985 MB,7683 文件)✓
- 现有 `backend_downloader.rs` 进度事件机制可复用 ✓
- `python_process.rs:111,115` env var 用法定位完成 ✓

**未跑通(企业网络三面拦截)**:
- PyPI:华为镜像缺 `numkong`,回退 `files.pythonhosted.org` 被代理篡改 → venv 不完整 → `build_backend.py` 打出 33 MB 空壳
- npm:Artifactory 缺 `@antv/component`、`@eslint/json`(404)→ node_modules 空 → `tauri-cli` 装不上 → `pnpm tauri build` 跑不了
- Rust:只有 GNU 工具链(`1.87.0-x86_64-pc-windows-gnu`),无 MSVC → Tauri 官方要 MSVC

**带走的产物**:T1+T2 代码改动 + 真实后端树已就位,换有完整网络的机器 `pnpm install && pnpm tauri build` 即可验证阶段 1。
