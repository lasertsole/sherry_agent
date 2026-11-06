# 打包桌面端启动可靠性

[**English**](README.md) · **中文** · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Tauri 统一安装包如何在 Windows 首次启动时保持可用：随包配置文件从 PyInstaller 冻结树中定位，QQ SDK 随后端打包而不是运行时 pip 安装，托盘关闭动作补齐窗口权限，客户端参数错误返回 400 而非 500。

## 首次配置播种

Shell 传入 `--sherry-home <app-data>`；`config/_home.resolve_code_root()` 对**随包模板**优先使用 `sys._MEIPASS`（onedir 的 `_internal` 目录），`ROOT_DIR` 继续承担项目/技能根目录的角色。`sherry.jsonc`、`.models`、`.network`、`.env` 在首次导入时从该模板根播种。模板缺失现在会写 warning 而不是静默跳过，打包布局异常可以直接从日志定位。

## 冻结环境与 QQ

冻结后端没有虚拟环境，无法向自身解释器安装包。`channels/deps.install_requirements()` 与 `channels/registry._ensure_deps()` 在 `sys.frozen` 为真时直接短路。QQ SDK（`botpy`，包名 `qq-botpy`）已加入项目依赖，并由 `packaging/build_backend.py` 收集，插件可正常导入。若某个构建漏带它，频道只写一条 INFO 并禁用自身，不会在每次启动时反复尝试 `uv pip install`。

## 托盘关闭行为

`CloseToTrayDialog` 通过 Tauri IPC 调用 `getCurrentWindow().hide()`。`core:default` 只授予只读窗口内省权限，因此 `client/src-tauri/capabilities/default.json` 显式加入 `core:window:allow-hide`。缺少它时，对话框确认了关闭但窗口仍停在原处；capability 编译进 Rust 外壳，必须重建安装包才能生效。

## HTTP 客户端参数错误

打包路由探测发现五个 handler 把客户端错误漏成了通用 500：媒体参数缺失/不安全、历史分页字段缺失或非整数、pending interrupt 缺 session、`DELETE /sessions` 缺 JSON body 或 session、memory entry id 非整数。现在都在 HTTP 边界返回统一的 `bad_request`/`not_found`（400/404）。

## 验证

聚焦测试覆盖冻结模板根解析和四个播种文件、冻结环境安装短路、QQ SDK 收集、托盘 capability，以及五处 400 响应。端到端首次启动仍需在全新 Windows 用户目录下重建安装包并实测。

来源：`config/_home.py`、`config/path.py`、`config/sherry_settings.py`、`config/models_settings.py`、`config/network_settings.py`、`channels/deps.py`、`channels/registry.py`、`plugins/channels/qq/core.py`、`packaging/build_backend.py`、`server/trigger/http/`、`client/src-tauri/capabilities/default.json`。
