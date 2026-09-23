# 代码检索框架 — 补充计划

> 基于 oh-my-openagent-dev 五层 LSP 安装基础设施和 ast-grep MCP 的深度调研，
> 补充 [`CODE_INTEL_SUBAGENT_PLAN.md`](CODE_INTEL_SUBAGENT_PLAN.md) 的五个缺口：
> LSP 二进制发现与自动安装、ast-grep 结构化搜索、LSP 语言/工具扩展、
> 文件事件自动同步、外部代码检索 subagent。
>
> **本文件是对原计划的修订与补充，不替代原计划。**
> 实施时原计划的 Phase 1 不变；Phase 2 由本文件 Phase 2S 替代；
> 新增 Phase 1A（必选，与 Phase 1 同级）、Phase 4 为独立阶段。
>
> **ast-grep 是核心必选组件**（对标 oh-my-openagent，在该项目中 ast-grep
> 与 LSP daemon 并列为无条件注册的核心组件，非 opt-in）。

---

## 目录

1. [缺口总览](#缺口总览)
2. [Phase 1A — ast-grep 结构化搜索（已落地）](#phase-1a--ast-grep-结构化搜索已落地)
3. [Phase 2S — LSP 二进制发现与自动安装（替代原 Phase 2）](#phase-2s--lsp-二进制发现与自动安装替代原-phase-2)
4. [Phase 2X — LSP 语言与工具扩展](#phase-2x--lsp-语言与工具扩展)
5. [Phase 4 — 外部代码检索 subagent](#phase-4--外部代码检索-subagent)
6. [Phase 5 — 文件事件自动同步（可选）](#phase-5--文件事件自动同步可选)
7. [修订后的总工期](#修订后的总工期)
8. [修改文件清单（新增）](#修改文件清单新增)
9. [测试计划（补充）](#测试计划补充)
10. [安全清单（补充）](#安全清单补充)

---

## 缺口总览

| #   | 缺口                                                         | oh-my-openagent 对标                                                    | 本计划阶段       |
| --- | ------------------------------------------------------------ | ----------------------------------------------------------------------- | ---------------- |
| 1   | LSP 二进制发现（repo-local → PATH → 安装提示）               | `server-installation.ts` (218 行)                                       | Phase 2S         |
| 2   | LSP 自动安装 + 安装提示 + 用户决策                           | `server-definitions.ts` + `install-decision.ts`                         | Phase 2S         |
| 3   | LSP fallback 策略（缺失时降级到 tree-sitter / search_files） | `server-resolution.ts` not_installed 状态                               | Phase 2S         |
| 4   | ast-grep 结构化搜索/重写                                     | `ast-grep-mcp/` (25 语言, 5 级 strictness)                              | Phase 1A（已落地） |
| 5   | LSP 语言覆盖（4 → 12+）                                      | `BUILTIN_SERVERS` (40+ 语言)                                            | Phase 2X         |
| 6   | LSP 工具覆盖（4 → 8）                                        | symbols/goto-def/refs/rename/diagnostics/format/status/install-decision | Phase 2X         |
| 7   | 外部代码检索（GitHub/npm/docs）                              | `librarian` subagent                                                    | Phase 4          |
| 8   | 文件事件自动同步                                             | CodeGraph 2s debounce 文件监听                                          | Phase 5 (可选)   |

---

## Phase 1A — ast-grep 结构化搜索（已落地）

> **状态：已落地。** 本节只保留落点、与提案的差异、实测摘要与平台覆盖；
> 规格细节已随实现移除。

### 落点

- `config/features/agent_side/ast_grep.py`：`AstGrepConfig` TypedDict + `AST_GREP` 实例，经
  `agent_side/__init__.py` 与 `config/features/__init__.py` **双级 re-export**。
- `agent/tools/code_intel/ast_grep/`：`resolver.py`（5 层发现：env → runtime → skill-bin →
  PATH → Homebrew，每层跑 `--version` 探针）、`provisioner.py`（SHA-256 校验的 GitHub
  release 下载 + stdlib `zipfile` 解压）、`install_hints.py`、`runner.py`
  （`ast_grep_search` / `ast_grep_rewrite`）、`__init__.py`。
- `skills/ast-grep/install.sh` + `install.ps1`：7 路包管理器 fallback
  （brew → npm → cargo → pip → nix → mise → GitHub ZIP）。
- `agent/tools/subagent/spawn/core.py::_build_child_agent()`：**所有 functional_role 的
  subagent 均注入** ast-grep；`ast_grep_*` 从不进入 `_MAIN_TOOLS_BUILDERS`，main agent 不可见。
- `.gitignore` 加 `skills/ast-grep/bin/`；运行时二进制写入
  `~/.sherry/runtime/ast-grep/<slug>/sg`，**不入库**。

### 与提案的差异

- **提取真实二进制而非 `sg` 启动器**：0.43.0 的 release ZIP 同时含 51 MB 的 `ast-grep`
  真二进制与约 440 KB 的 `sg` 启动器；启动器按自身路径 re-exec，在受限 PRoot 沙箱中失败。
  provisioner 因此**始终优先提取 `ast-grep`**，再以 resolver 期望的 `sg` 名写入运行时目录。
- **rewrite 语义修正**：`sg run` 没有 `--dry-run` 选项。dry-run 用 `--json=stream`
  （只预览、不写盘，且与 `-U` 互斥）；apply 用 `--update-all`，其 "Applied N changes"
  输出走 **stderr**（解析 stdout + stderr 两路）。
- **路径安全自持**：子代理中间件链不注册 `PathGuard`，`runner.py` 因此自行用
  `resolve_project_path`（生产）/ `SHERRY_SG_ROOT`（沙箱）校验每条路径；遍历或越界一律拒绝，
  `dry_run=False` 也只能写项目根内。
- **未改 `spawn/system_prompt.py`**：提案文件清单列了「追加 ast-grep 使用指导」，实际未做
  （工具描述已自解释），属有意省略。
- **实测摘要与平台覆盖**：release 无 checksums 资产，故 6 平台 SHA-256 均由**官方 release
  资产实际下载后计算**，6 个值全部验证（darwin/win32/linux × arm64/x64）。本机平台为
  **linux-arm64**（非提案假定的 x86_64）。实测 linux-arm64 归档摘要
  `e706846148493967f3ab8011334817edd86ce5acbec10718b2a7b40799c640ff`（与配置一致）；
  provision 实测 2.7 s，产出 51,531,936 B / 0755 的 `sg`。

## Phase 2S — LSP 二进制发现与自动安装（替代原 Phase 2）

> **前置条件：Phase 1 + Phase 1A 完成。**
> **替代原计划 Phase 2。** 原计划的 4 个 LSP 工具保留不变，
> 本阶段补充原计划完全缺失的 LSP 服务器发现、安装、fallback 基础设施。

### 架构

```
┌─ LSP 服务器发现层 ──────────────────────────────────────────────┐
│                                                                  │
│  resolve_lsp_server(language, cwd)                               │
│  ├─ 1. 显式路径（LSP_*_SERVER 配置为绝对路径）                    │
│  ├─ 2. repo-local 二进制（.venv/bin, node_modules/.bin, ...）     │
│  │     └─ marker-gated: package.json / pyproject.toml / go.mod   │
│  ├─ 3. PATH 查找（含 Windows PATHEXT 后缀匹配）                   │
│  └─ 4. not_installed → 返回安装提示 + fallback 信号               │
│                                                                  │
│  install_lsp_server(language)                                     │
│  ├─ 查 AUTO_INSTALLABLE_SERVERS 获取安装命令                      │
│  ├─ 执行安装（subprocess, 超时 60s, 日志捕获）                     │
│  └─ 安装后重新 resolve_lsp_server                                  │
│                                                                  │
│  LSP fallback 链                                                  │
│  ├─ LSP 可用 → lsp_goto_definition / lsp_find_references          │
│  ├─ LSP 不可用 → tree-sitter explore (Phase 1)                    │
│  └─ tree-sitter 无匹配 → search_files (regex grep)                │
└──────────────────────────────────────────────────────────────────┘
```

### 新增文件（4 个）

#### 1. `config/features/agent_side/lsp.py`（扩展原计划的 LspConfig）

```python
"""LSP server configuration — binary paths, timeouts, install hints."""

from typing import TypedDict


class LspConfig(TypedDict):
    """Configuration for LSP tools and server provisioning."""
    # 原计划字段（保留）
    lsp_python_server: str
    lsp_typescript_server: str
    lsp_rust_server: str
    lsp_go_server: str
    lsp_request_timeout_s: float
    lsp_server_start_timeout_s: float
    lsp_enabled_languages: list[str]
    # ── 补充字段 ──
    lsp_install_timeout_s: float           # 自动安装超时（默认 60）
    lsp_auto_install: bool                 # 是否允许自动安装（默认 False，需用户授权）
    lsp_resolve_cache_enabled: bool        # 二进制发现缓存（默认 True）
    lsp_repo_local_bin_rules: dict         # marker → bin_dirs 映射
    lsp_supported_servers: dict            # 语言 → server 配置
    lsp_install_hints: dict                # 语言 → 安装提示文本
    lsp_auto_install_commands: dict        # 语言 → 安装命令列表


LSP: LspConfig = {
    # 原计划字段（保留）
    "lsp_python_server": "basedpyright-langserver",
    "lsp_typescript_server": "typescript-language-server",
    "lsp_rust_server": "rust-analyzer",
    "lsp_go_server": "gopls",
    "lsp_request_timeout_s": 10.0,
    "lsp_server_start_timeout_s": 15.0,
    "lsp_enabled_languages": ["python", "typescript", "rust", "go"],
    # ── 补充字段 ──
    "lsp_install_timeout_s": 60.0,
    "lsp_auto_install": False,
    "lsp_resolve_cache_enabled": True,
    # repo-local bin 目录发现规则（参考 oh-my-openagent server-installation.ts）
    # marker 文件存在时才信任对应的 bin 目录
    "lsp_repo_local_bin_rules": {
        "python": {
            "markers": [
                "pyproject.toml", "requirements.txt", "setup.py",
                "setup.cfg", "pyrightconfig.json",
            ],
            "bin_dirs": [".venv/bin", ".venv/Scripts", "venv/bin", "venv/Scripts"],
        },
        "typescript": {
            "markers": [
                "package.json", "bun.lock", "package-lock.json",
                "yarn.lock", "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
        "rust": {
            "markers": ["Cargo.toml"],
            "bin_dirs": ["target/debug", "target/release"],
        },
        "go": {
            "markers": ["go.mod", "go.sum", "go.work"],
            "bin_dirs": ["bin"],
        },
    },
    # 语言 → LSP server 命令 + 文件扩展名
    "lsp_supported_servers": {
        "python": {
            "command": ["basedpyright-langserver", "--stdio"],
            "extensions": [".py", ".pyi"],
            "local_install": "uv add --dev basedpyright",
        },
        "typescript": {
            "command": ["typescript-language-server", "--stdio"],
            "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"],
            "local_install": "bun add -d typescript-language-server typescript",
        },
        "rust": {
            "command": ["rust-analyzer"],
            "extensions": [".rs"],
            "local_install": None,  # 工具链级，无项目级安装
        },
        "go": {
            "command": ["gopls"],
            "extensions": [".go"],
            "local_install": None,
        },
    },
    # 安装提示（not_installed 时返回给 subagent / 用户）
    "lsp_install_hints": {
        "python": "pip install basedpyright",
        "typescript": "npm install -g typescript-language-server typescript",
        "rust": "rustup component add rust-analyzer",
        "go": "go install golang.org/x/tools/gopls@latest",
    },
    # 自动安装命令（lsp_auto_install=True 且用户授权时执行）
    "lsp_auto_install_commands": {
        "python": ["pip", "install", "basedpyright"],
        "typescript": ["npm", "install", "-g", "typescript-language-server", "typescript"],
        "go": ["go", "install", "golang.org/x/tools/gopls@latest"],
        # rust-analyzer 经由 rustup，不走 pip/npm，不列入自动安装
    },
}
```

**遵循约定：** 一个 TypedDict + 一个实例，re-export via `__init__.py`。

#### 2. `agent/tools/code_intel/lsp/resolver.py` — 二进制发现

```python
"""LSP server binary resolution — multi-layer discovery with caching.

Probing order (mirrors oh-my-openagent server-installation.ts):
  1. Explicit path (config value is absolute) → validate exists
  2. Repo-local bin dirs (marker-gated: pyproject.toml → .venv/bin, package.json → node_modules/.bin)
  3. PATH lookup (with Windows PATHEXT suffixes)
  4. None → not_installed (caller returns install hint + fallback)

Results cached per-process, keyed by (cwd, command, platform).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from config.features import LSP

_resolution_cache: dict[str, str | None] = {}


def _executable_suffixes() -> list[str]:
    """Platform-appropriate executable suffixes."""
    if sys.platform != "win32":
        return [""]
    pathext = os.environ.get("PATHEXT", "")
    suffixes = [e.lower() for e in pathext.split(";") if e]
    return list(dict.fromkeys(["", *suffixes, ".exe", ".cmd", ".bat"]))


def _probe(directory: str, command: str, suffixes: list[str]) -> str | None:
    for suffix in suffixes:
        candidate = Path(directory) / (command + suffix)
        if candidate.exists():
            return str(candidate)
    return None


def _resolve_local(
    command: str, cwd: str, suffixes: list[str], language: str
) -> str | None:
    """Walk up from cwd, checking marker-gated bin dirs."""
    rules = LSP["lsp_repo_local_bin_rules"].get(language, {})
    markers = rules.get("markers", [])
    bin_dirs = rules.get("bin_dirs", [])

    current = Path(cwd).resolve()
    while True:
        # Check if any marker exists at this level
        if any((current / m).exists() for m in markers):
            for bin_dir in bin_dirs:
                found = _probe(str(current / bin_dir), command, suffixes)
                if found:
                    return found

        # Stop at repo root
        if (current / ".git").exists():
            return None

        parent = current.parent
        if parent == current:
            return None
        current = parent


def _resolve_from_path(command: str, suffixes: list[str]) -> str | None:
    path_env = os.environ.get("PATH") or os.environ.get("Path") or ""
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        found = _probe(entry, command, suffixes)
        if found:
            return found
    return None


def resolve_lsp_server(language: str, cwd: str | None = None) -> str | None:
    """Resolve LSP server binary path, or None if not installed.

    Args:
        language: One of LSP["lsp_enabled_languages"].
        cwd: Working directory for repo-local resolution.

    Returns:
        Absolute binary path, or None.
    """
    server_config = LSP["lsp_supported_servers"].get(language)
    if not server_config:
        return None
    command = server_config["command"][0]
    work_dir = cwd or os.getcwd()

    if not LSP["lsp_resolve_cache_enabled"]:
        return _resolve_uncached(command, work_dir, language)

    cache_key = f"{work_dir}:{command}:{sys.platform}"
    if cache_key in _resolution_cache:
        return _resolution_cache[cache_key]

    result = _resolve_uncached(command, work_dir, language)
    _resolution_cache[cache_key] = result
    return result


def _resolve_uncached(command: str, work_dir: str, language: str) -> str | None:
    suffixes = _executable_suffixes()

    # 1. Explicit absolute path
    if os.path.isabs(command):
        return command if Path(command).exists() else None

    # 2. Repo-local bin dirs (marker-gated)
    local = _resolve_local(command, work_dir, suffixes, language)
    if local:
        return local

    # 3. PATH
    return _resolve_from_path(command, suffixes)


def get_install_hint(language: str) -> str:
    """Return human-readable install hint for a language's LSP server."""
    return LSP["lsp_install_hints"].get(
        language, f"Install the LSP server for {language} and ensure it's in PATH."
    )


def get_local_install_hint(language: str) -> str | None:
    """Return repo-local install command (devDependency), or None."""
    server = LSP["lsp_supported_servers"].get(language, {})
    return server.get("local_install")


def _reset_cache_for_tests() -> None:
    _resolution_cache.clear()
```

#### 3. `agent/tools/code_intel/lsp/installer.py` — 自动安装

```python
"""LSP server auto-install — subprocess execution with timeout + log capture."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from loguru import logger

from config.features import LSP
from agent.tools.pub_base.env_scrub import scrub_env
from .resolver import resolve_lsp_server, get_install_hint


def install_lsp_server(language: str, cwd: str | None = None) -> dict:
    """Install an LSP server for the given language.

    Returns:
        {"ok": bool, "binary_path": str | None, "message": str}
    """
    if not LSP["lsp_auto_install"]:
        hint = get_install_hint(language)
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Auto-install disabled. Install manually: {hint}",
        }

    command = LSP["lsp_auto_install_commands"].get(language)
    if not command:
        hint = get_install_hint(language)
        return {
            "ok": False,
            "binary_path": None,
            "message": f"No auto-install command for {language}. Install manually: {hint}",
        }

    timeout = LSP["lsp_install_timeout_s"]
    env = scrub_env(os.environ.copy())

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd(),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install timed out after {timeout}s: {' '.join(command)}",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install tool not found: {command[0]}. Is it on PATH?",
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install failed (exit {proc.returncode}): {proc.stderr[:500]}",
        }

    logger.info("LSP server installed for {}: {}", language, " ".join(command))

    # Re-resolve after install
    binary_path = resolve_lsp_server(language, cwd)
    # Clear cache so re-resolution sees the new binary
    from .resolver import _reset_cache_for_tests
    _reset_cache_for_tests()
    binary_path = resolve_lsp_server(language, cwd)

    return {
        "ok": binary_path is not None,
        "binary_path": binary_path,
        "message": f"Installed {language} LSP server" if binary_path else "Install ran but binary not found on PATH",
    }
```

#### 4. `agent/tools/code_intel/lsp/fallback.py` — fallback 链

```python
"""LSP fallback chain — graceful degradation when LSP is unavailable.

Resolution order:
  1. LSP server available → use LSP tools (goto_definition, find_references, ...)
  2. LSP not installed → return install hint + suggest tree-sitter explore (Phase 1)
  3. tree-sitter no match → suggest search_files (regex grep)
"""

from __future__ import annotations

from typing import Literal

from .resolver import resolve_lsp_server, get_install_hint, get_local_install_hint


type LspAvailability = Literal["available", "not_installed", "not_configured"]


def check_lsp_availability(language: str, cwd: str | None = None) -> tuple[LspAvailability, str]:
    """Check if LSP is available for a language.

    Returns:
        (status, message)
        - ("available", binary_path)
        - ("not_installed", install_hint)
        - ("not_configured", "Language {language} not in enabled list")
    """
    from config.features import LSP as LSP_CONFIG

    if language not in LSP_CONFIG["lsp_enabled_languages"]:
        return "not_configured", f"Language '{language}' not in LSP enabled list: {LSP_CONFIG['lsp_enabled_languages']}"

    binary = resolve_lsp_server(language, cwd)
    if binary:
        return "available", binary

    hint = get_install_hint(language)
    local_hint = get_local_install_hint(language)
    if local_hint:
        hint = f"{hint} (project-local: {local_hint})"
    return "not_installed", hint


def build_fallback_message(
    language: str,
    requested_tool: str,
    availability: LspAvailability,
    message: str,
) -> str:
    """Build a user-facing message when LSP is unavailable.

    Suggests the fallback path:
      LSP unavailable → try explore (tree-sitter) → try search_files
    """
    if availability == "not_configured":
        return (
            f"{requested_tool}: {message}. "
            f"Fallback: use `explore` (tree-sitter symbol index) or `search_files` (regex)."
        )
    if availability == "not_installed":
        return (
            f"{requested_tool}: LSP server for {language} is not installed. "
            f"Install: {message}. "
            f"Fallback: use `explore` (tree-sitter) or `search_files` (regex) for now."
        )
    return ""
```

### 原计划 Phase 2 文件（保留不变）

原计划的以下文件保留，但需修改 `client.py` 以使用 `resolver.py` 发现的二进制路径：

| 文件                                     | 修改                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------- |
| `agent/tools/code_intel/lsp/protocol.py` | 不变                                                                                        |
| `agent/tools/code_intel/lsp/client.py`   | 启动前调用 `resolve_lsp_server()` 发现二进制；not_installed 时调用 `fallback.py`            |
| `agent/tools/code_intel/lsp/tools.py`    | 每个工具调用前检查 `check_lsp_availability()`，not_installed 时返回安装提示 + fallback 建议 |

### `client.py` 修改要点

```python
# 原计划 client.py 直接用 config 中的 server 名启动子进程
# 修改后：先 resolve_lsp_server() 发现二进制，再启动

from .resolver import resolve_lsp_server, check_lsp_availability
from .fallback import build_fallback_message

class LSPClient:
    def __init__(self, language: str, cwd: str):
        self._language = language
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> str | None:
        """Start LSP server. Returns error message (None = success)."""
        availability, msg = check_lsp_availability(self._language, self._cwd)
        if availability != "available":
            return build_fallback_message(
                self._language, "lsp_start", availability, msg
            )
        # resolve_lsp_server 返回绝对路径
        binary_path = msg  # "available" 状态下 msg = binary path
        server_config = LSP["lsp_supported_servers"][self._language]
        command = [binary_path, *server_config["command"][1:]]

        env = scrub_env(os.environ.copy())
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
        )
        ...
```

### `tools.py` 修改要点

```python
# 每个工具在执行 LSP 请求前检查可用性

@tool("lsp_goto_definition")
def _lsp_goto_definition_tool(
    file_path: str, line: int, character: int, session_id: str = ""
) -> str:
    """Jump to the definition of a symbol."""
    language = _detect_language(file_path)
    availability, msg = check_lsp_availability(language)
    if availability != "available":
        return build_fallback_message(
            language, "lsp_goto_definition", availability, msg
        )
    # ... proceed with LSP request ...
```

---

## Phase 2X — LSP 语言与工具扩展

> **前置条件：Phase 2S 完成。**
> **目标：** 将 LSP 覆盖从 4 语言 + 4 工具扩展到 12+ 语言 + 8 工具。

### 新增语言

在 `LSP["lsp_supported_servers"]` 中追加：

| 语言  | LSP Server             | 安装命令                                              | 文件扩展名                 |
| ----- | ---------------------- | ----------------------------------------------------- | -------------------------- |
| C/C++ | `clangd`               | `See https://clangd.llvm.org/installation`            | `.c .cpp .cc .cxx .h .hpp` |
| Java  | `jdtls`                | `See https://github.com/eclipse-jdtls/eclipse.jdt.ls` | `.java`                    |
| Ruby  | `ruby-lsp`             | `gem install ruby-lsp`                                | `.rb .rake`                |
| Bash  | `bash-language-server` | `npm install -g bash-language-server`                 | `.sh .bash .zsh`           |
| Vue   | `vue-language-server`  | `npm install -g @vue/language-server`                 | `.vue`                     |
| YAML  | `yaml-language-server` | `npm install -g yaml-language-server`                 | `.yaml .yml`               |

> **设计约束：** 不预装所有 LSP server。`lsp_enabled_languages` 由用户配置控制，
> 默认仅启用已安装的语言。`resolve_lsp_server()` 发现二进制存在时自动启用。

### 新增工具

| 工具              | LSP 方法                          | 说明                                    | 优先级 |
| ----------------- | --------------------------------- | --------------------------------------- | ------ |
| `lsp_rename`      | `textDocument/rename`             | 跨工作区符号重命名                      | P1     |
| `lsp_diagnostics` | `textDocument/publishDiagnostics` | 实时诊断（错误/警告）                   | P1     |
| `lsp_format`      | `textDocument/rangeFormatting`    | 代码格式化                              | P2     |
| `lsp_status`      | — (内部聚合)                      | 列出所有已配置/已安装/活跃的 LSP 服务器 | P2     |

> `lsp_rename` 和 `lsp_diagnostics` 是代码编辑中最常用的 LSP 功能，
> 缺少它们意味着 subagent 无法完成"重构前检查"和"重命名"工作流。

### 工期

| 步骤     | 内容                                                                             | 预估    |
| -------- | -------------------------------------------------------------------------------- | ------- |
| 1        | 扩展 `lsp_supported_servers` / `lsp_install_hints` / `lsp_auto_install_commands` | 0.5h    |
| 2        | `lsp/protocol.py` 追加 rename / diagnostics 数据结构                             | 1h      |
| 3        | `lsp/tools.py` 追加 4 个工具                                                     | 3h      |
| 4        | 修改 `spawn/core.py` + `system_prompt.py`（注入新工具）                          | 0.3h    |
| 5        | 测试                                                                             | 2h      |
| 6        | ruff + basedpyright + pytest                                                     | 0.5h    |
| **小计** |                                                                                  | **~7h** |

---

## Phase 4 — 外部代码检索 subagent

> **前置条件：subagent 功能角色分工（已落地）+ 本计划 Phase 1A 完成。**
> **定位：** 对标 oh-my-openagent 的 `librarian` subagent。
> 专门检索**外部代码库**（GitHub 仓库、npm 包、官方文档），
> 与 Phase 1-2 的**内部**代码检索互补。

### 架构

```
┌─ Librarian Subagent（RESEARCHER 角色，只读）──────────────────────┐
│                                                                    │
│  输入：用户问"某库怎么用？" / "某库怎么实现的？"                     │
│                                                                    │
│  工具链（复用现有 + 新增）：                                         │
│  ├─ web_search          已有 — 搜索官方文档 URL                      │
│  ├─ web_fetch           已有 — 抓取文档页面                           │
│  ├─ terminal (git/gh)   已有 — clone repo, git blame, gh search      │
│  ├─ search_files        已有 — 搜索 clone 下来的仓库                  │
│  ├─ read_file           已有 — 读取 clone 的源码                     │
│  └─ explore (Phase 1)   已有 — tree-sitter 索引 clone 的仓库         │
│                                                                    │
│  输出：                                                              │
│  ├─ 概念问题 → 官方文档摘要 + 链接                                   │
│  ├─ 实现问题 → 源码引用 + GitHub permalink                          │
│  └─ 上下文问题 → 相关 issues/PRs                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 设计决策

- **不新增工具** — librarian 复用现有的 `web_search`、`terminal`（git/gh）、`search_files`、`read_file`、`explore`
- **新增的是 subagent 角色定义** — 在 `FunctionalRole` 中定义 `RESEARCHER` 角色时，librarian 是一个预设的 RESEARCHER 实例
- **新增 `librarian` system prompt** — 类似 oh-my-openagent 的 librarian prompt，包含文档发现 → 仓库 clone → 源码搜索的工作流

### 新增文件（1 个）

#### `agent/tools/subagent/spawn/role_definitions/librarian.py`

```python
"""Librarian subagent role definition — external codebase retrieval.

Registered as a RESEARCHER-role subagent preset. The spawn system resolves
this definition when `agent_id="librarian"` is passed to sessions_spawn.
"""

from __future__ import annotations

LIBRARIAN_SYSTEM_PROMPT = """\
# THE LIBRARIAN

You are a specialized open-source codebase understanding agent.

## Your Mission

Answer questions about external libraries by finding EVIDENCE with
GitHub permalinks or official documentation links.

## Workflow

### TYPE A: Conceptual ("How do I use X?")
  1. web_search("library official documentation")
  2. web_fetch(specific doc page)
  3. Summarize with version-aware links

### TYPE B: Implementation ("How does X implement Y?")
  1. terminal: git clone --depth 1 to temp dir
  2. explore or search_files in cloned repo
  3. read_file for specific implementation
  4. Construct GitHub permalink: https://github.com/owner/repo/blob/<sha>/path#L10-L20

### TYPE C: Context ("Why was X changed?")
  1. terminal: gh search issues/prs
  2. terminal: git log --oneline -- path
  3. terminal: git blame -L start,end path

## Rules
- ALWAYS cite with permalinks (include commit SHA)
- Use --depth 1 for clones unless history is needed
- Clean up temp clones when done
- Read-only: never modify files in the target repo
"""

LIBRARIAN_TOOL_ALLOW = [
    "web_search", "web_fetch", "terminal", "read_file",
    "search_files", "explore", "callers", "callees", "impact",
    "ast_grep_search",
]

LIBRARIAN_TOOL_DENY = [
    "write", "edit", "patch_file", "task", "call_omo_agent",
    "sessions_spawn", "sessions_yield", "sessions_send",
]
```

### 注入点

在角色定义加载器中注册：

```python
# agent/tools/subagent/spawn/role_definitions/__init__.py
from .librarian import LIBRARIAN_SYSTEM_PROMPT, LIBRARIAN_TOOL_ALLOW, LIBRARIAN_TOOL_DENY

ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    # ... existing roles ...
    "librarian": RoleDefinition(
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
        tool_allow=LIBRARIAN_TOOL_ALLOW,
        tool_deny=LIBRARIAN_TOOL_DENY,
        functional_role=FunctionalRole.RESEARCHER,
    ),
}
```

### 工期

| 步骤     | 内容                                           | 预估      |
| -------- | ---------------------------------------------- | --------- |
| 1        | `role_definitions/librarian.py`                | 1h        |
| 2        | 注册到角色定义加载器                           | 0.3h      |
| 3        | 测试（spawn librarian subagent, 验证工具权限） | 1h        |
| **小计** |                                                | **~2.5h** |

---

## Phase 5 — 文件事件自动同步（可选）

> **前置条件：Phase 1 完成。**
> **定位：** 对标 oh-my-openagent CodeGraph 的 2s debounce 文件监听。
> 当前 sherry 的 tree-sitter 索引仅在查询时按 mtime 增量更新，
> 无实时性。本阶段添加文件监听 + 后台增量重索引。

### 设计

```
┌─ 文件监听器（进程级，server 启动时启动）──────────────────────────┐
│                                                                    │
│  watchfiles / asyncio 监听 ROOT_DIR（排除 prune_dirs）              │
│  ├─ 文件变更事件 → debounce 2s                                     │
│  ├─ debounce 后 → 增量重索引变更文件（mtime 检查）                  │
│  └─ 写入 .codeintel/db.sqlite                                      │
│                                                                    │
│  与查询路径的关系：                                                  │
│  └─ explore/callers/callees/impact 查询时仍做 mtime 检查            │
│     （监听器可能落后，mtime 是最终一致性保障）                       │
└────────────────────────────────────────────────────────────────────┘
```

### 依赖

```toml
# pyproject.toml [project] dependencies — 新增
"watchfiles>=2.0",  # Rust-based file watcher (asyncio)
```

### 新增文件（1 个）

#### `agent/tools/code_intel/watcher.py`

```python
"""Background file watcher for incremental re-indexing.

Started by the server process (not by subagents). Watches ROOT_DIR,
debounces file events, and triggers incremental re-indexing of changed
files in the .codeintel/db.sqlite symbol index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from watchfiles import awatch

from config.features import CODE_INTEL
from config.path import ROOT_DIR
from .indexer import CodeIndexer


async def start_index_watcher(stop_event: asyncio.Event) -> None:
    """Start background file watcher for code intel index.

    Called once at server startup. Runs until stop_event is set.
    """
    if not CODE_INTEL.get("code_intel_index_db_path"):
        return  # code intel not configured

    indexer = CodeIndexer(
        CODE_INTEL["code_intel_index_db_path"],
        CODE_INTEL,
    )
    prune_dirs = set(CODE_INTEL["code_intel_prune_dirs"])
    debounce_s = 2.0

    async for changes in awatch(
        str(ROOT_DIR),
        stop_event=stop_event,
        watch_filter=lambda change, path: not any(
            p in path for p in prune_dirs
        ),
    ):
        # Debounce: collect changes, wait 2s, then batch re-index
        await asyncio.sleep(debounce_s)
        changed_files = [
            Path(path) for _, path in changes
            if Path(path).suffix in CODE_INTEL["code_intel_supported_extensions"]
        ]
        if changed_files:
            try:
                indexer.index_files(changed_files, ROOT_DIR)
                logger.debug("Re-indexed {} files", len(changed_files))
            except Exception:
                logger.exception("Background re-index failed")
```

### 注入点

在 `server/__main__.py` 或 `server/service/` 中启动：

```python
# server startup
import asyncio
from agent.tools.code_intel.watcher import start_index_watcher

stop_event = asyncio.Event()
# Start watcher as background task
asyncio.create_task(start_index_watcher(stop_event))
# On shutdown: stop_event.set()
```

### 工期

| 步骤     | 内容                                  | 预估    |
| -------- | ------------------------------------- | ------- |
| 1        | `pyproject.toml` 加 `watchfiles` 依赖 | 0.1h    |
| 2        | `agent/tools/code_intel/watcher.py`   | 2h      |
| 3        | 修改 server 启动逻辑                  | 0.5h    |
| 4        | 测试                                  | 1.5h    |
| **小计** |                                       | **~4h** |

---

## 修订后的总工期

| 阶段     | 内容                                             | 预估       | 前置            |
| -------- | ------------------------------------------------ | ---------- | --------------- |
| 前置     | subagent 功能角色分工（已落地）                  | —          | —               |
| Phase 1  | tree-sitter 符号索引 + 调用图 (原计划)           | ~15h       | 前置            |
| Phase 1A | ast-grep 结构化搜索 + 二进制 provision (已落地)  | —          | 前置            |
| Phase 2S | LSP 二进制发现 + 自动安装 + fallback (替代原 P2) | ~16h       | Phase 1 + 1A    |
| Phase 2X | LSP 语言 + 工具扩展 (新增)                       | ~7h        | Phase 2S        |
| Phase 3  | Embedding 语义搜索 (原计划)                      | ~8h        | Phase 1         |
| Phase 4  | 外部代码检索 subagent (新增)                     | ~2.5h      | Phase 1A + 前置 |
| Phase 5  | 文件事件自动同步 (可选, 新增)                    | ~4h        | Phase 1         |
| **合计** |                                                  | **~65.5h** |                 |

> 原计划 ~34h → 修订后 ~65.5h。增量 ~31.5h 主要来自 ast-grep 二进制 provision
> 基础设施（Phase 1A, +13h）和 LSP 基础设施（Phase 2S, +5h vs 原 Phase 2 的 ~11h → ~16h）。

### 推荐实施顺序

```
前置 (subagent 功能角色分工)
  ├→ Phase 1 (tree-sitter, 15h)
  └→ Phase 1A (ast-grep + provision) ← 已落地，与 Phase 1 并行
       ├→ Phase 2S (LSP 基础设施, 16h) ← 需 Phase 1 + 1A
       │    └→ Phase 2X (LSP 扩展, 7h)
       ├→ Phase 3 (Embedding, 8h) ← 可与 Phase 2S 并行
       └→ Phase 4 (librarian, 2.5h) ← 可与 Phase 2S 并行
            Phase 5 (file watcher, 4h) ← 可选，最后
```

---

## 修改文件清单（新增）

### Phase 1A 修改（已落地）

| 文件                                               | 修改                                          |
| -------------------------------------------------- | --------------------------------------------- |
| `config/features/agent_side/ast_grep.py`           | 新建（含 6 平台 SHA-256 provision 配置）      |
| `config/features/agent_side/__init__.py`           | re-export `AST_GREP` / `AstGrepConfig`        |
| `config/features/__init__.py`                      | 顶层 re-export                                |
| `agent/tools/code_intel/ast_grep/__init__.py`      | 新建                                          |
| `agent/tools/code_intel/ast_grep/resolver.py`      | 新建 — 5 层二进制发现 + `--version` 探针      |
| `agent/tools/code_intel/ast_grep/provisioner.py`   | 新建 — SHA-256 校验下载 + 提取真二进制        |
| `agent/tools/code_intel/ast_grep/install_hints.py` | 新建 — 安装提示                               |
| `agent/tools/code_intel/ast_grep/runner.py`        | 新建 — `ast_grep_search` / `ast_grep_rewrite` |
| `skills/ast-grep/install.sh`                       | 新建 — POSIX 安装脚本（7 路 fallback）        |
| `skills/ast-grep/install.ps1`                      | 新建 — Windows 安装脚本                       |
| `agent/tools/subagent/spawn/core.py`               | 注入 ast_grep（**所有 subagent**）            |
| `.gitignore`                                       | 加 `skills/ast-grep/bin/`                     |

> 提案清单中的 `agent/tools/subagent/spawn/system_prompt.py` 改动未执行（见「与提案的差异」）。

### Phase 2S 修改（5 个）

| 文件                                      | 修改                               |
| ----------------------------------------- | ---------------------------------- |
| `config/features/agent_side/lsp.py`       | 扩展 LspConfig（+7 字段 + 3 dict） |
| `agent/tools/code_intel/lsp/resolver.py`  | 新建 — 二进制发现                  |
| `agent/tools/code_intel/lsp/installer.py` | 新建 — 自动安装                    |
| `agent/tools/code_intel/lsp/fallback.py`  | 新建 — fallback 链                 |
| `agent/tools/code_intel/lsp/client.py`    | 修改 — 用 resolver 发现二进制      |
| `agent/tools/code_intel/lsp/tools.py`     | 修改 — 每个工具调用前检查可用性    |

### Phase 2X 修改（2 个）

| 文件                                          | 修改                                   |
| --------------------------------------------- | -------------------------------------- |
| `config/features/agent_side/lsp.py`           | 扩展 `lsp_supported_servers` (+6 语言) |
| `agent/tools/code_intel/lsp/protocol.py`      | 追加 rename/diagnostics 数据结构       |
| `agent/tools/code_intel/lsp/tools.py`         | 追加 4 个工具                          |
| `agent/tools/subagent/spawn/core.py`          | 注入新工具                             |
| `agent/tools/subagent/spawn/system_prompt.py` | 追加 LSP 工具指导                      |

### Phase 4 修改（2 个）

| 文件                                                       | 修改           |
| ---------------------------------------------------------- | -------------- |
| `agent/tools/subagent/spawn/role_definitions/librarian.py` | 新建           |
| `agent/tools/subagent/spawn/role_definitions/__init__.py`  | 注册 librarian |

### Phase 5 修改（3 个）

| 文件                                      | 修改                  |
| ----------------------------------------- | --------------------- |
| `pyproject.toml`                          | 加 `watchfiles>=2.0`  |
| `agent/tools/code_intel/watcher.py`       | 新建                  |
| `server/__main__.py` 或 `server/service/` | 启动 watcher 后台任务 |

### 公共修改

| 文件             | 修改                                                       |
| ---------------- | ---------------------------------------------------------- |
| `config/path.py` | 加 `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"` (原计划已有) |
| `.gitignore`     | 加 `.codeintel/` (原计划已有)                              |

---

## 测试计划（补充）

| 文件                                                        | 阶段 | 标记          | 覆盖点                                                                                  |
| ----------------------------------------------------------- | ---- | ------------- | --------------------------------------------------------------------------------------- |
| `tests/agent/tools/code_intel/ast_grep/test_resolver.py`      | P1A  | `unit`   | 5 层发现（env/runtime/skill-bin/PATH/homebrew）、--version 探针、缓存、Windows 后缀       |
| `tests/agent/tools/code_intel/ast_grep/test_provisioner.py`   | P1A  | `unit`   | SHA-256 校验、优先提取 `ast-grep`、6 平台 asset、下载/校验/超时/解压失败                   |
| `tests/agent/tools/code_intel/ast_grep/test_install_hints.py` | P1A  | `unit`   | 平台安装提示、config 双级 re-export、TypedDict 键一致                                     |
| `tests/agent/tools/code_intel/ast_grep/test_runner.py`        | P1A  | `unit`   | sg 子进程、JSON 解析、退出码、超时、dry_run/apply rewrite、路径安全、SHERRY_SG_PATH       |
| `tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py`  | P1A  | `module` | 四个 functional_role 均获 ast-grep、main 不可见、假下载器 hermetic e2e（search+rewrite）  |
| `tests/agent/tools/code_intel/lsp/test_resolver.py`         | P2S  | `unit`        | 三层发现（显式路径/repo-local/PATH）、缓存命中、Windows 后缀、marker-gated bin 目录     |
| `tests/agent/tools/code_intel/lsp/test_installer.py`        | P2S  | `unit`        | 自动安装命令执行、超时、returncode、安装后重发现、auto_install=False 拒绝               |
| `tests/agent/tools/code_intel/lsp/test_fallback.py`         | P2S  | `unit`        | available/not_installed/not_configured 三态、fallback 消息格式、语言检测                |
| `tests/agent/tools/code_intel/lsp/test_lsp_tools.py`        | P2S  | `integration` | 4 LSP 工具端到端 + not_installed fallback 路径                                          |
| `tests/agent/tools/code_intel/lsp/test_lsp_extended.py`     | P2X  | `integration` | lsp_rename / lsp_diagnostics / lsp_format / lsp_status                                  |
| `tests/agent/tools/subagent/test_librarian_role.py`         | P4   | `integration` | librarian 角色定义加载、工具权限正确（有 explore/无 write）                             |
| `tests/agent/tools/code_intel/test_watcher.py`              | P5   | `integration` | 文件变更触发重索引、debounce 2s、prune_dirs 排除                                        |

### 关键测试用例

```python
# test_resolver.py (LSP)
def test_resolve_local_venv_bin():
    """pyproject.toml 存在时 .venv/bin 被探测。"""

def test_resolve_local_node_modules():
    """package.json 存在时 node_modules/.bin 被探测。"""

def test_resolve_from_path():
    """PATH 上的二进制被发现。"""

def test_resolve_not_installed():
    """二进制不存在时返回 None。"""

def test_resolve_cache_hit():
    """相同参数的第二次调用命中缓存，不重复探测文件系统。"""

def test_marker_gate():
    """无 marker 文件时 bin 目录不被信任。"""

# test_installer.py (LSP)
def test_auto_install_disabled():
    """lsp_auto_install=False 时返回安装提示而非执行安装。"""

def test_auto_install_timeout():
    """安装超时返回明确错误。"""

def test_auto_install_success():
    """安装成功后 resolve_lsp_server 能发现新二进制。"""

# test_fallback.py (LSP)
def test_not_installed_returns_hint():
    """LSP 未安装时返回安装提示 + fallback 建议。"""

def test_not_configured_returns_list():
    """语言不在 enabled list 时返回 not_configured。"""

def test_available_returns_binary():
    """LSP 已安装时返回 available + binary path。"""
```

---

## 安全清单（补充）

| 维度                                       | 措施                                                               | 阶段 |
| ------------------------------------------ | ------------------------------------------------------------------ | ---- |
| **ast-grep 二进制 provision SHA-256 校验** | 下载的 ZIP 经 SHA-256 校验后才解压，防止供应链篡改                 | P1A  |
| **ast-grep 二进制版本锁定**                | pinned 0.43.0，不从 PATH 接受任意版本，--version 探针验证          | P1A  |
| **ast-grep 子进程隔离**                    | sg CLI 通过 subprocess.run 启动，有超时（30s），不继承敏感环境变量 | P1A  |
| **ast-grep rewrite dry_run**               | 默认 dry_run=True，必须显式设置 false 才修改文件                   | P1A  |
| **LSP 子进程环境清洗**                     | LSP server 子进程环境经 `scrub_env()` 清洗，不泄漏 API keys        | P2S  |
| **LSP 自动安装用户授权**                   | `lsp_auto_install` 默认 False，需用户在配置中显式开启              | P2S  |
| **LSP 安装命令白名单**                     | 仅 `lsp_auto_install_commands` 中列出的命令可执行，不接受任意命令  | P2S  |
| **LSP 安装超时**                           | 60s 超时，防止安装命令挂起                                         | P2S  |
| **LSP 二进制路径可信**                     | resolve_local 仅在 marker 文件存在时信任 bin 目录，防止目录注入    | P2S  |
| **文件监听排除敏感目录**                   | watcher 排除 prune_dirs（.git, .venv, node_modules, ...）          | P5   |
| **文件监听仅触发索引**                     | watcher 不执行代码，仅 parse + 写 SQLite                           | P5   |
| **librarian 临时仓库清理**                 | clone 到 temp 目录，subagent 结束时清理                            | P4   |
| **librarian 只读约束**                     | tool_deny 包含 write/edit/patch_file，无法修改文件                 | P4   |
| **librarian 无 spawn 权限**                | tool_deny 包含 sessions_spawn/yield/send，无法创建子 subagent      | P4   |

---

## 关键参考文件

### Sherry 内部（复用）

| 文件                                    | 用途                                                               |
| --------------------------------------- | ------------------------------------------------------------------ |
| `agent/tools/pub_base/env_scrub.py`     | `scrub_env()` — LSP/sg 子进程环境清洗                              |
| `agent/tools/file_tools/search_scan.py` | `bounded_walk` + `ScanState` — 索引遍历复用                        |
| `agent/tools/web_search.py`             | `build_web_search_tool()` — librarian 复用                         |
| `agent/tools/terminal.py`               | terminal 工具 — librarian git/gh 操作复用                          |
| `agent/tools/file_tools/read_file.py`   | `build_read_file_tool()` — librarian 源码读取复用                  |
| `context_engine/embeddings/store.py`    | Embedding BLOB 存储（Phase 3 复用，原计划已有）                    |
| `config/path.py` `ROOT_DIR`             | ast-grep skill bin cache 路径基础 (`ROOT_DIR/skills/ast-grep/bin`) |

### 外部参考

| 项目                  | 文件                                         | 参考内容                                                                      |
| --------------------- | -------------------------------------------- | ----------------------------------------------------------------------------- |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-installation.ts`    | 三层二进制发现（repo-local → PATH → Windows 后缀）                            |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-definitions.ts`     | `AUTO_INSTALLABLE_SERVERS` + `LSP_INSTALL_HINTS` + `LSP_LOCAL_INSTALL_HINTS`  |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-resolution.ts`      | `findServerForExtension()` not_installed 状态 + installHint 返回              |
| `oh-my-openagent-dev` | `lsp-core/src/tools/install-decision.ts`     | `lsp_install_decision` 工具（用户授权安装）                                   |
| `oh-my-openagent-dev` | `lsp-core/src/tools/status.ts`               | `lsp_status` 工具（服务器状态聚合）                                           |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/search.ts`           | sg CLI 参数构建 + 输出解析                                                    |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/rewrite.ts`          | dry_run rewrite 实现                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-resolver.ts`          | 5 层二进制发现（env/runtime/skill-bin/PATH/homebrew）+ --version 探针         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-provisioner.ts`       | ast-grep 二进制 SHA-256 验证下载 + ZIP 解压                                   |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-candidates.ts`        | 5 层候选路径生成逻辑                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-install-hints.ts`     | 平台特定的安装提示（brew/npm/cargo/scoop/winget/choco）                       |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-manifest.ts`          | pinned 版本号 + 6 平台 SHA-256 校验值                                         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/install-script.ts`       | install.sh / install.ps1 启动器（30s 超时, Windows pwsh→powershell fallback） |
| `oh-my-openagent-dev` | `shared-skills/skills/ast-grep/install.sh`   | 286 行 7 路 fallback 安装脚本（brew/npm/cargo/pip/nix/mise/github）           |
| `oh-my-openagent-dev` | `omo-senpi/src/components/ast-grep/index.ts` | ast-grep MCP server 注册（enabled: true, lifecycle: "lazy"）                  |
| `oh-my-openagent-dev` | `omo-senpi/src/extension/component-list.ts`  | ast-grep 无条件注册（与 LSP 并列）                                            |
| `oh-my-openagent-dev` | `agents/librarian.ts`                        | librarian system prompt + 工具权限                                            |
| `oh-my-openagent-dev` | `utils/src/codegraph/provision.ts`           | 二进制 auto-provisioning 模式（SHA-256 验证）                                 |
| `deepagents-main`     | `libs/code/deepagents_code/managed_tools.py` | managed ripgrep 自动安装 + SHA-256 验证模式                                   |
