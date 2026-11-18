# Packaged-app E2E (WebDriver)

15 个核心功能的真实操作桩：驱动**打包后的 Tauri app**（deb 解包布局），使用 app
自带的真实 Python 后端，模型指向本地 OpenAI 兼容 mock。

> 为什么不是 Playwright：Linux 的 WebKitGTK 不暴露 CDP，Playwright 无法附着打包
> WebView。Tauri release 二进制支持 W3C WebDriver，`tauri-driver` +
> `WebKitWebDriver` 可以直接驱动它，不需要改 app 或打开 devtools。

## 前置依赖（测试机，不进安装包）

```bash
# 方式 A：有 root
sudo apt-get install -y webkit2gtk-driver
cargo install tauri-driver --locked

# 方式 B：无 root（当前开发环境使用）
apt-get download webkit2gtk-driver
dpkg-deb -x webkit2gtk-driver_*.deb /tmp/sherry-e2e-deps/root
cargo install tauri-driver --locked   # 若安装后的二进制被环境 shim 破坏，
                                      # 从 cargo 临时目录复制真实 ELF 到 /dev/shm 使用
```

另外需要：Xvfb（或已有 `DISPLAY`）、`dpkg-deb`、Python 3、约 1.5 GB 临时磁盘。

## 构建打包 app

```bash
uv run --no-sync python packaging/build_backend.py
cd client && pnpm tauri build
# 产物：client/src-tauri/target/release/bundle/deb/*.deb
```

runner 默认取最新的 deb，解包到临时前缀并执行 `<prefix>/usr/bin/ema-ai-agent`。
`--app` 只能指向**安装版或 deb 解包布局**的二进制；不要指向
`target/release/ema-ai-agent`：Tauri 的资源目录会解析到
`/usr/lib/EMA AI Agent`（或 `target/lib/...`），裸二进制找不到 `sherry-backend/`，
前端会显示 Backend service unreachable。

## 当前状态

在本机 Linux arm64 / Xvfb `:99` / WebKitWebDriver 2.52.6 / tauri-driver 2.1.0
上，对 **deb 解包后的实际 app** 实跑：**15/15 PASS**。这次实跑同时抓出并修复了
三个打包链路缺陷：Rust bridge 的 agent WS 未带 gateway token、HITL 帧未转发到
前端、以及 mock 工具结果后的非流式响应。详见 `report.json` 和
`e2e-report/packaged-*/summary.txt`。

## 运行

```bash
cd client

pnpm test:e2e:packaged:list          # 15 个 feature
pnpm test:e2e:packaged:mock          # mock 模型 SSE/工具/错误自检
pnpm test:e2e:packaged:spike         # 单会话启动冒烟 + 截图
pnpm test:e2e:packaged               # 15 项全跑

# 只跑一项
pnpm test:e2e:packaged -- --feature F05

# 自定义二进制/驱动/显示
pnpm test:e2e:packaged -- \
  --app /tmp/sherry-e2e-deb/usr/bin/ema-ai-agent \
  --tauri-driver "$HOME/.cargo/bin/tauri-driver" \
  --webkit-driver /usr/bin/WebKitWebDriver \
  --display :99 --keep
```

环境变量：`TAURI_DRIVER_BIN`、`WEBKIT_WEBDRIVER_BIN`、`SHERRY_E2E_PYTHON`。
退出码：`0` 全 PASS，`1` 有 FAIL，`2` 环境/设置 BLOCKED 或有 SKIP。

## 一次 run 做了什么

1. 新建临时根目录，`XDG_DATA_HOME=<root>/XDG_DATA_HOME`；
2. 启动 `mock_llm.py`（随机端口），写入
   `<root>/XDG_DATA_HOME/com.ema-ai.agent/.models`：
   - `MAIN_LLM_PROVIDER=openai`
   - `MAIN_LLM_API_BASE=http://127.0.0.1:<mock-port>/v1`
   - `MAIN_LLM_MAX_TOKEN=200000`（启动门槛）
   - `AUXILIARY_LLM_*` 指向同一个 mock；
3. 写 `sherry.jsonc` 的 `project_dir` 和 `workspace/e2e-fixture.txt`；
4. 启动 `tauri-driver`（`browserName=wry`，application=解包后的 app）；
5. 建立 WebDriver 会话，关 onboarding、切英文、创建首个会话；
6. 串行跑 feature，失败截图并继续；
7. `quit_app`、杀 backend 孤儿、停 mock/driver/Xvfb；
8. 报告写到 `client/e2e-report/packaged-<timestamp>/`（`report.json` +
   `summary.txt` + 截图 + 日志）；成功默认删临时目录，失败/`--keep` 保留。

隔离要点：每次都是全新 `XDG_DATA_HOME`，不会读用户真实 DB/workspace；但打包前端
编译期写死后端 `http://localhost:8080`，所以 **8080 必须空闲**。runner 会在启动前
检查并直接报错。

## Mock 模型协议

`mock_llm.py` 是 stdlib-only 的 OpenAI 兼容服务端：

| prompt 标记    | 行为                                                                 |
| -------------- | -------------------------------------------------------------------- |
| `[e2e:plain]`  | 3 段文本流 + stop                                                    |
| `[e2e:stream]` | 0.7 s/段的多段流                                                     |
| `[e2e:tools]`  | `read_file {path:"e2e-fixture.txt"}`，收到 tool 结果后给最终文本     |
| `[e2e:hitl]`   | `terminal {command:"echo e2e-approved"}`，收到 tool 结果后给最终文本 |
| `[e2e:error]`  | HTTP 500（后端重试后 UI 出现 Reply failed）                          |
| `[e2e:slow]`   | 1.2 s/段的慢流，供 Stop 测试                                         |

自检：`python3 e2e/packaged/mock_llm.py --selftest`。

## 15 个功能

| ID  | 功能       | 用户动作                                          | 断言                                        |
| --- | ---------- | ------------------------------------------------- | ------------------------------------------- |
| F01 | 发送按钮   | 输入，点 Send                                     | assistant 最终文本                          |
| F02 | Enter 发送 | 输入，按 Enter                                    | assistant 最终文本                          |
| F03 | 流式回复   | `[e2e:stream]`                                    | 先部分、后完整                              |
| F04 | 工具卡     | `[e2e:tools]`，展开工具卡                         | `read_file` 卡片 + 最终回答                 |
| F05 | HITL 审批  | 访问模式 Confirm changes → `[e2e:hitl]` → Approve | 工具执行后最终文本                          |
| F06 | 错误帧     | `[e2e:error]`                                     | `Reply failed`                              |
| F07 | 停止生成   | `[e2e:slow]` → Stop                               | Stop 消失、Send 恢复、保留部分回答          |
| F08 | 新建会话   | New Chat → sherry → Create                        | URL/侧栏新会话                              |
| F09 | 切换会话   | 建第二个会话，点第一个                            | URL 切回                                    |
| F10 | 删除会话   | 删除一次性会话 → Confirm Delete                   | 行消失                                      |
| F11 | 设置菜单   | 点 Menu                                           | System Config / Skills / Memory             |
| F12 | 系统配置   | Menu → System Config                              | System Config / Model Config                |
| F13 | 模型选择   | 点 `.model-trigger`                               | Main model / Follow the model config        |
| F14 | 上传入口   | 点 `.insert-trigger`                              | 四种上传入口                                |
| F15 | 连接健康   | 启动后                                            | 无 backend-down/offline 横幅，composer 可用 |

## 已知限制

- 仅 Linux（WebKitGTK）。Windows 可用 WebView2 的
  `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222` +
  Playwright CDP，但本次未实现；macOS 不支持 tauri-driver。
- 打包 app 的前端 API base 是编译期 `http://localhost:8080`；不要同时开开发后端。
- 测试机需要 `webkit2gtk-driver` 和 `tauri-driver`；它们不是 app 依赖，不进 deb。
- 当前不加入 CI（需要桌面/Xvfb、WebDriver 和约 500 MB 的打包产物）。
- 只覆盖 15 个核心功能；新增功能按 `features/fNN-*.mjs` + `features/index.mjs`
  的同一模式追加。

## 故障排查

- **Backend service unreachable**：确认用的是 deb/安装版（不是裸
  `target/release` 二进制），并检查 `<temp>/XDG_DATA_HOME/com.ema-ai.agent/logs/`；
- **WebDriver 会话创建失败**：看报告目录的 `logs/tauri-driver.log`；PRoot/容器
  下需要 `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` 等环境（runner 已设置）；
- **端口占用**：先停掉 8080 上的后端/app；
- **驱动被环境 shim 破坏**：`tauri-driver`/`WebKitWebDriver` 必须是真实可执行文件，
  不是指向 `.l2s.*` 的坏 symlink；可用 `readlink -f` 检查。
