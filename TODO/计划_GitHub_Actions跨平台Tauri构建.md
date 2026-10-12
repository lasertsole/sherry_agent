# 计划：GitHub Actions 跨平台 Tauri 构建 + 后端分离下载

- 建档日期：2026-10-09
- 状态：**计划（已批准，未开工）**
- 触发问题：sherry 的 Tauri 桌面打包机制已可用（PyInstaller onedir + Tauri bundle），但零 CI——没有 build workflow、bundle targets 只有 Linux、无 artifact 上传。用户要求 main 分支每次提交时 GitHub 自动打包 mac/window/linux 三平台 Tauri 应用

## 用户选择

- **macOS**：只 arm64（macos-latest）
- **后端分离**：Tauri 只打前端 + Rust shell（~30-50 MiB），Python 后端单独压缩包放 GitHub Releases（~3-4 GiB），首次启动自动下载解压
- **触发**：push 到 main = 构建 + 上传 artifact（临时）；打 tag = 创建 GitHub Release（永久）

## 首次启动流程

```
用户安装 Tauri installer（~30 MiB）→ 打开应用
  ↓
Rust shell 启动 → 检查 <app_data>/sherry-backend/ 是否存在
  ↓ 不存在
下载 sherry-backend-{platform}.tar.gz 从 GitHub Releases latest
  ↓ 前端显示"正在下载后端…"进度条
解压到 <app_data>/sherry-backend/
  ↓
spawn 后端 → 前端加载正常界面
  ↓
后续启动：后端已存在 → 直接 spawn（秒开）
```

## 改动清单

### 1. 新建 `.github/workflows/desktop-build.yml`

三平台 matrix（ubuntu-latest / windows-latest / macos-latest），两个并行 job + 一个 release job。

#### Job 1: `build-backend`（慢，~20 min，可缓存）

```yaml
build-backend:
  strategy:
    matrix:
      include:
        - os: ubuntu-latest
          platform: linux
          arch: x86_64
          archive: tar.gz
        - os: windows-latest
          platform: windows
          arch: x86_64
          archive: zip
        - os: macos-latest
          platform: macos
          arch: arm64
          archive: tar.gz
  runs-on: ${{ matrix.os }}
  steps:
    - uses: actions/checkout@v4

    # Python + uv
    - uses: astral-sh/setup-uv@v6
      with:
        enable-cache: true
        cache-dependency-glob: uv.lock
    - run: uv python install 3.13

    # 安装全部依赖（含 torch CPU-only，不含 CUDA——桌面应用不需要 GPU 推理）
    - run: uv sync --frozen
      env:
        UV_INDEX_URL: https://download.pytorch.org/whl/cpu  # CPU-only torch

    # PyInstaller 构建
    - run: uv run --with pyinstaller python packaging/build_backend.py

    # 生成 VERSION 文件（Rust shell 用于版本比对）
    - run: |
        echo "${{ github.ref_name }}" > client/src-tauri/sherry-backend/VERSION
      shell: bash

    # 压缩后端 onedir
    - name: Compress backend (Linux/macOS)
      if: matrix.archive == 'tar.gz'
      run: |
        cd client/src-tauri/sherry-backend
        tar czf ${{ github.workspace }}/sherry-backend-${{ matrix.platform }}-${{ matrix.arch }}.tar.gz .
    - name: Compress backend (Windows)
      if: matrix.archive == 'zip'
      run: |
        cd client/src-tauri/sherry-backend
        7z a -tzip ${{ github.workspace }}\sherry-backend-${{ matrix.platform }}-${{ matrix.arch }}.zip .

    # 删除 onedir 释放磁盘（压缩包已生成）
    - run: rm -rf client/src-tauri/sherry-backend
      shell: bash

    - uses: actions/upload-artifact@v4
      with:
        name: sherry-backend-${{ matrix.platform }}-${{ matrix.arch }}
        path: sherry-backend-${{ matrix.platform }}-${{ matrix.arch }}.${{ matrix.archive }}
        retention-days: 90
```

#### Job 2: `build-tauri`（快，~5 min）

```yaml
build-tauri:
  strategy:
    matrix:
      include:
        - os: ubuntu-latest
          platform: linux
        - os: windows-latest
          platform: windows
        - os: macos-latest
          platform: macos
  runs-on: ${{ matrix.os }}
  steps:
    - uses: actions/checkout@v4

    # Linux 系统依赖
    - name: Install Linux deps
      if: matrix.platform == 'linux'
      run: |
        sudo apt update
        sudo apt install -y libwebkit2gtk-4.1-dev libssl-dev patchelf libayatana-appindicator3-dev

    # Rust
    - uses: dtolnay/rust-toolchain@stable
    - uses: swatinem/rust-cache@v2
      with:
        workspaces: client/src-tauri

    # Node + pnpm
    - uses: pnpm/action-setup@v4
      with:
        version: 11
    - uses: actions/setup-node@v4
      with:
        node-version: 22
        cache: pnpm
        cache-dependency-path: client/pnpm-lock.yaml

    - run: pnpm install --frozen-lockfile
      working-directory: client

    # Tauri 构建（beforeBuildCommand 自动跑 nuxt generate）
    - run: pnpm tauri build
      working-directory: client

    # 上传安装包
    - uses: actions/upload-artifact@v4
      with:
        name: tauri-installer-${{ matrix.platform }}
        path: |
          client/src-tauri/target/release/bundle/**/*.deb
          client/src-tauri/target/release/bundle/**/*.AppImage
          client/src-tauri/target/release/bundle/**/*.msi
          client/src-tauri/target/release/bundle/**/*.exe
          client/src-tauri/target/release/bundle/**/*.dmg
          client/src-tauri/target/release/bundle/**/*.app
```

#### Job 3: `release`（仅 tag 触发）

```yaml
release:
  if: startsWith(github.ref, 'refs/tags/')
  needs: [build-backend, build-tauri]
  runs-on: ubuntu-latest
  permissions:
    contents: write
  steps:
    - uses: actions/download-artifact@v4
      with:
        path: artifacts

    - uses: softprops/action-gh-release@v2
      with:
        generate_release_notes: true
        files: |
          artifacts/sherry-backend-*/*
          artifacts/tauri-installer-*/*
```

#### 触发条件

```yaml
on:
  push:
    branches: [main]     # 构建 + 上传 artifact（临时 90 天）
    tags: ['v*']          # 构建 + 创建 GitHub Release（永久）
  workflow_dispatch:      # 手动触发
```

#### 缓存策略

| 缓存 | Key | 作用 |
|---|---|---|
| `uv` cache | `uv-{os}-{hash(uv.lock)}` | 跳过 torch 等 ~8 GiB 下载 |
| PyInstaller work dir | `pyinstaller-{os}-{hash(pyproject.toml)}` | 跳过 PyInstaller 分析 |
| pnpm store | `pnpm-{os}-{hash(pnpm-lock.yaml)}` | 跳过 node_modules 下载 |
| Rust target | `rust-{os}-{hash(Cargo.lock)}` | 跳过 Rust 增量编译 |

### 2. 修改 `client/src-tauri/tauri.conf.json`

```json
{
  "bundle": {
    "active": true,
    "targets": "all",
    "icon": ["icons/32x32.png", "icons/128x128.png", "icons/128x128@2x.png", "icons/icon.icns", "icons/icon.ico"],
    "resources": {}
  }
}
```

- `targets`: `["deb", "appimage"]` → `"all"`（每平台自动选原生 bundler：Linux=deb+appimage，Windows=nsis+msi，macOS=dmg+app）
- `resources`: 删除 `"sherry-backend/": "sherry-backend/"`（后端不再打包进 installer）

### 3. 新建 `client/src-tauri/src/services/backend_downloader.rs`（~150 行 Rust）

首次启动下载后端的逻辑：

```rust
//! 首次启动时从 GitHub Releases 下载 Python 后端。
//!
//! 检查 <app_home>/sherry-backend/ 是否存在。
//! 不存在 → 下载平台对应的压缩包 → 解压到 app_home。
//! 已存在且版本匹配 → 跳过。
//! 已存在但版本旧 → 重新下载。

use std::path::{Path, PathBuf};
use tauri::Emitter;

const GITHUB_REPO: &str = "user/sherry_agent";  // TODO: 改为实际 repo

pub struct BackendDownloader {
    app_home: PathBuf,
    repo: String,
}

impl BackendDownloader {
    pub fn new(app_home: PathBuf, repo: String) -> Self {
        Self { app_home, repo }
    }

    /// 后端目录：<app_home>/sherry-backend/
    pub fn backend_dir(&self) -> PathBuf {
        self.app_home.join("sherry-backend")
    }

    /// 后端可执行文件路径
    pub fn backend_path(&self) -> PathBuf {
        let exe = if cfg!(windows) { "sherry-backend.exe" } else { "sherry-backend" };
        self.backend_dir().join(exe)
    }

    /// 后端是否已存在
    pub fn backend_exists(&self) -> bool {
        self.backend_path().is_file()
    }

    /// 下载 URL（平台 + 架构 + 格式）
    fn download_url(&self) -> String {
        let (os, arch, ext) = if cfg!(target_os = "windows") {
            ("windows", "x86_64", "zip")
        } else if cfg!(target_os = "macos") {
            ("macos", "arm64", "tar.gz")
        } else {
            ("linux", "x86_64", "tar.gz")
        };
        format!(
            "https://github.com/{}/releases/latest/download/sherry-backend-{}-{}.{}",
            self.repo, os, arch, ext
        )
    }

    /// 检查版本是否需要更新（对比 VERSION 文件）
    fn needs_update(&self, current_version: &str) -> bool {
        let version_file = self.backend_dir().join("VERSION");
        if let Ok(stored) = std::fs::read_to_string(&version_file) {
            return stored.trim() != current_version;
        }
        true  // 无 VERSION 文件 → 需要下载
    }

    /// 确保后端存在 + 版本匹配。不存在或版本旧则下载。
    pub fn ensure_backend(
        &self,
        app: &tauri::AppHandle,
        current_version: &str,
    ) -> Result<PathBuf, String> {
        if self.backend_exists() && !self.needs_update(current_version) {
            return Ok(self.backend_path());
        }

        let url = self.download_url();
        tracing::info!("Downloading backend from {}", url);

        // 1. 流式下载到临时文件
        let tmp_archive = self.app_home.join("sherry-backend-download.tmp");
        let total_size = download_with_progress(&url, &tmp_archive, app)?;

        // 2. 清理旧后端目录
        let _ = std::fs::remove_dir_all(self.backend_dir());
        std::fs::create_dir_all(self.backend_dir())
            .map_err(|e| format!("Failed to create backend dir: {}", e))?;

        // 3. 解压
        if cfg!(target_os = "windows") {
            extract_zip(&tmp_archive, &self.backend_dir())?;
        } else {
            extract_tar_gz(&tmp_archive, &self.backend_dir())?;
        }

        // 4. 设置可执行权限（Unix）
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let perms = std::fs::Permissions::from_mode(0o755);
            let _ = std::fs::set_permissions(self.backend_path(), perms);
        }

        // 5. 清理临时文件
        let _ = std::fs::remove_file(&tmp_archive);

        // 6. 通知前端下载完成
        let _ = app.emit("backend-download-complete", ());

        Ok(self.backend_path())
    }
}

/// 流式下载 + emit 进度事件
fn download_with_progress(
    url: &str,
    dest: &Path,
    app: &tauri::AppHandle,
) -> Result<u64, String> {
    use std::io::{Read, Write};

    let response = ureq::get(url)
        .call()
        .map_err(|e| format!("Download failed: {}", e))?;

    let total = response.header("Content-Length")
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(0);

    let mut file = std::fs::File::create(dest)
        .map_err(|e| format!("Failed to create temp file: {}", e))?;

    let mut downloaded: u64 = 0;
    let mut buffer = [0u8; 65536];  // 64 KiB chunks

    loop {
        let read = response.into_reader().read(&mut buffer)
            .map_err(|e| format!("Download read error: {}", e))?;
        if read == 0 { break; }

        file.write_all(&buffer[..read])
            .map_err(|e| format!("Write error: {}", e))?;

        downloaded += read as u64;
        let percent = if total > 0 { (downloaded * 100 / total) as u8 } else { 0 };

        let _ = app.emit("backend-download-progress", serde_json::json!({
            "downloaded": downloaded,
            "total": total,
            "percent": percent,
        }));
    }

    file.flush().map_err(|e| format!("Flush error: {}", e))?;
    Ok(total)
}

/// 解压 tar.gz（Linux/macOS）
fn extract_tar_gz(archive: &Path, dest: &Path) -> Result<(), String> {
    let output = std::process::Command::new("tar")
        .arg("xzf")
        .arg(archive)
        .arg("-C")
        .arg(dest)
        .output()
        .map_err(|e| format!("tar extraction failed: {}", e))?;
    if !output.status.success() {
        return Err(format!("tar failed: {}", String::from_utf8_lossy(&output.stderr)));
    }
    Ok(())
}

/// 解压 zip（Windows）—— Windows 10+ 内建 tar 支持 zip
fn extract_zip(archive: &Path, dest: &Path) -> Result<(), String> {
    let output = std::process::Command::new("tar")
        .arg("xf")
        .arg(archive)
        .arg("-C")
        .arg(dest)
        .output()
        .map_err(|e| format!("zip extraction failed: {}", e))?;
    if !output.status.success() {
        return Err(format!("zip extract failed: {}", String::from_utf8_lossy(&output.stderr)));
    }
    Ok(())
}
```

**关键设计点**：

- **平台 + 架构自动识别**：`cfg!(target_os)` + `cfg!(target_arch)` 编译期决定下载 URL
- **版本管理**：后端压缩包里带 `VERSION` 文件，Rust shell 比对版本——installer 版本 > 已有后端版本 → 重新下载
- **进度推送**：`app.emit("backend-download-progress", ...)` → 前端监听事件显示进度条
- **解压**：Linux/macOS 用系统 `tar xzf`；Windows 用系统 `tar xf`（Windows 10+ 内建 tar 支持 zip 格式）—— 不需要额外 Rust crate
- **可执行权限**：Unix 上 `chmod 755` 后端可执行文件
- **fail-open**：下载失败 → 返回 Error，前端显示重试按钮

**Cargo.toml 依赖**：

```toml
[dependencies]
# 已有：tauri, tauri-plugin-notification, tauri-plugin-dialog, tauri-plugin-window-state, tauri-plugin-single-instance, tauri-plugin-global-shortcut, tracing, tracing-subscriber, serde, serde_json
# 新增：
ureq = "2"          # 轻量 HTTP 客户端（无 async runtime 依赖，同步下载够用）
```

不需要 `zip` crate——用系统 `tar` 命令解压（Windows 10+ 内建）。不需要 `reqwest`——`ureq` 更轻（无 async runtime）。

### 4. 修改 `client/src-tauri/src/lib.rs`（~20 行改动）

在 `setup()` 里，先检查后端是否存在，不存在则下载：

```rust
// ── Python backend process ─────
let app_home = app.path().app_data_dir()...;
std::fs::create_dir_all(&app_home)?;

// 新增：首次启动检查后端
let app_version = env!("CARGO_PKG_VERSION");  // 从 Cargo.toml 读版本
let downloader = BackendDownloader::new(
    app_home.clone(),
    GITHUB_REPO.to_string(),
);

let backend_exe = if downloader.backend_exists() && !downloader.needs_update(app_version) {
    // 已存在 + 版本匹配 → 直接用
    downloader.backend_path()
} else {
    // 不存在或版本旧 → 下载
    match downloader.ensure_backend(app.handle(), app_version) {
        Ok(path) => path,
        Err(e) => {
            tracing::error!("Backend download failed: {}", e);
            // fallback: 尝试 checkout 模式（开发环境）
            let project_root = std::env::var("EMA_PROJECT_ROOT")...;
            // ... 现有 Checkout fallback 逻辑
        }
    }
};

let command = BackendCommand::Packaged(backend_exe);
let process_mgr = PythonProcessManager::new(command, app_home.clone());
```

**移除**对 `resource_dir` 的 `packaged_backend()` 探测——后端不再在 resource_dir 里，而在 app_home 里。

**保留** `BackendCommand::Checkout` 路径——开发环境（`pnpm tauri dev`）仍然用 `python -m server`。

### 5. 前端：下载进度页面（~50 行 Vue）

在 `client/app/app.vue` 或新建 `client/app/components/BackendDownloadProgress.vue`：

```vue
<template>
  <Transition name="fade">
    <div v-if="downloading" class="backend-download-overlay">
      <div class="download-card">
        <h2>正在下载 Sherry 后端</h2>
        <p class="download-hint">首次启动需要下载后端组件（约 3-4 GiB），完成后即可使用。</p>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: percent + '%' }" />
        </div>
        <p class="progress-text">
          {{ downloadedMB }} / {{ totalMB }} MB ({{ percent }}%)
        </p>
        <button v-if="error" class="retry-btn" @click="retry">重试下载</button>
        <p v-if="error" class="error-text">{{ error }}</p>
      </div>
    </div>
  </Transition>
</template>

<script setup lang="ts">
const downloading = ref(false)
const percent = ref(0)
const downloadedMB = ref(0)
const totalMB = ref(0)
const error = ref('')

listen('backend-download-progress', (e) => {
  downloading.value = true
  error.value = ''
  percent.value = e.payload.percent
  downloadedMB.value = Math.round(e.payload.downloaded / 1024 / 1024)
  totalMB.value = Math.round(e.payload.total / 1024 / 1024)
})

listen('backend-download-complete', () => {
  downloading.value = false
})

listen('backend-download-error', (e) => {
  error.value = e.payload.error
})
</script>
```

### 6. `packaging/build_backend.py` 小改

在 PyInstaller 构建完成后，生成 VERSION 文件：

```python
# 在 build_backend.py 末尾，build 完成后加：
version = _read_version()  # 从 tauri.conf.json 或 git tag
version_file = onedir_path / "VERSION"
version_file.write_text(version, encoding="utf-8")
logger.info("Wrote VERSION file: {} = {}", version_file, version)

def _read_version() -> str:
    """从 git tag 或 tauri.conf.json 读版本号。"""
    # 优先 git tag
    import subprocess
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # fallback: tauri.conf.json 的 version 字段
    import json
    conf = Path(__file__).parent.parent / "client" / "src-tauri" / "tauri.conf.json"
    if conf.is_file():
        data = json.loads(conf.read_text(encoding="utf-8"))
        return data.get("version", "0.0.0")
    return "0.0.0"
```

### 7. 产物清单（tag 触发时）

| 文件 | 平台 | 大小 | 用途 |
|---|---|---|---|
| `EMA-AI-Agent_0.1.0_amd64.deb` | Linux x86_64 | ~30 MiB | Debian/Ubuntu 安装包 |
| `EMA-AI-Agent_0.1.0_amd64.AppImage` | Linux x86_64 | ~30 MiB | 免安装可执行 |
| `EMA-AI-Agent_0.1.0_x64-setup.exe` | Windows x86_64 | ~30 MiB | NSIS 安装包 |
| `EMA-AI-Agent_0.1.0_x64.msi` | Windows x86_64 | ~30 MiB | MSI 安装包 |
| `EMA-AI-Agent_0.1.0_aarch64.dmg` | macOS arm64 | ~30 MiB | macOS 安装包 |
| `sherry-backend-linux-x86_64.tar.gz` | Linux | ~3-4 GiB | 首次启动下载 |
| `sherry-backend-windows-x86_64.zip` | Windows | ~3-4 GiB | 首次启动下载 |
| `sherry-backend-macos-arm64.tar.gz` | macOS | ~3-4 GiB | 首次启动下载 |

## 不做的事

- **不做代码签名**——macOS notarization / Windows Authenticode 是 follow-up。未签名的 app 在 macOS 需右键打开，Windows 有 SmartScreen 警告
- **不做 macOS x86_64**——macos-latest 是 arm64。Intel Mac 用户后续加 macos-13 job
- **不做 Tauri updater**——不加自动更新机制。用户手动下载新版本 installer
- **不做后端增量更新**——每次下载完整后端压缩包。不做 diff/patch 更新
- **不做 self-hosted runner**——用 GitHub hosted runner。分离后端已解决磁盘问题（Tauri build 不含后端 ~30 MiB；backend build 单独跑，构建后压缩再删 onedir 释放磁盘）

## 关键文件路径

| 文件 | 角色 | 行数 |
|---|---|---|
| `.github/workflows/desktop-build.yml` | 新建：CI workflow | ~120 |
| `client/src-tauri/tauri.conf.json` | 修改：targets→all，删 resources | ~3 |
| `client/src-tauri/src/services/backend_downloader.rs` | 新建：下载 + 解压 + 进度 | ~150 |
| `client/src-tauri/src/lib.rs` | 修改：setup 里调 downloader | ~20 改动 |
| `client/src-tauri/Cargo.toml` | 修改：加 `ureq` crate | ~2 |
| `client/app/components/BackendDownloadProgress.vue` | 新建：下载进度 UI | ~50 |
| `packaging/build_backend.py` | 修改：生成 VERSION 文件 | ~15 |
