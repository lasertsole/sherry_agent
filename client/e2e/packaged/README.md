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
上，对 **deb 解包后的实际 app** 实跑：**62/62 PASS**（F01–F15 核心 + F16–F63
扩容覆盖）。扩容实测同样抓出并修复了真实缺陷：

- Tauri bridge 的 agent WS 未带 gateway token（聊天全被拒）；
- `hitl_request` 帧未转发到前端（审批卡片永不出现）；
- 部分打包 UI 缺稳定定位点（会话行 `data-session-id`、菜单 tile、编辑入口）。

已知不在 WebDriver 覆盖内（人工或后续专项验证）：原生 OS 文件夹对话框（打包
app 的 "Choose folder…" 走 Tauri dialog，测试桩改用后端 API 做 fixture 绑定）、
原生 OS 文件选择器、托盘/原生菜单、语音 STT 全链路（无权重时只验证按钮降级
路径）、浏览器 CDP 驱动的页面内容。

报告与截图：`e2e-report/packaged-<run_id>/report.json`、`summary.txt`。

## 运行

```bash
cd client

pnpm test:e2e:packaged:list          # 15 个 feature
pnpm test:e2e:packaged:mock          # mock 模型 SSE/工具/错误自检
pnpm test:e2e:packaged:spike         # 单会话启动冒烟 + 截图
pnpm test:e2e:packaged               # 15 项全跑

# 只跑一项 / 一组（逗号分隔）
pnpm test:e2e:packaged -- --feature F05
pnpm test:e2e:packaged -- --feature F16,F17,F18

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

## 62 个功能

| ID  | 功能         | 用户动作                                          | 断言                                         |
| --- | ------------ | ------------------------------------------------- | -------------------------------------------- |
| F01 | 发送按钮     | 输入，点 Send                                     | assistant 最终文本                           |
| F02 | Enter 发送   | 输入，按 Enter                                    | assistant 最终文本                           |
| F03 | 流式回复     | `[e2e:stream]`                                    | 先部分、后完整                               |
| F04 | 工具卡       | `[e2e:tools]`，展开工具卡                         | `read_file` 卡片 + 最终回答                  |
| F05 | HITL 审批    | 访问模式 Confirm changes → `[e2e:hitl]` → Approve | 工具执行后最终文本                           |
| F06 | 错误帧       | `[e2e:error]`                                     | `Reply failed`                               |
| F07 | 停止生成     | `[e2e:slow]` → Stop                               | Stop 消失、Send 恢复、保留部分回答           |
| F08 | 新建会话     | New Chat → sherry → Create                        | URL/侧栏新会话                               |
| F09 | 切换会话     | 建第二个会话，点第一个                            | URL 切回                                     |
| F10 | 删除会话     | 删除一次性会话 → Confirm Delete                   | 行消失                                       |
| F11 | 设置菜单     | 点 Menu                                           | System Config / Skills / Memory              |
| F12 | 系统配置     | Menu → System Config                              | System Config / Model Config                 |
| F13 | 模型选择     | 点 `.model-trigger`                               | Main model / Follow the model config         |
| F14 | 上传入口     | 点 `.insert-trigger`                              | 四种上传入口                                 |
| F15 | 连接健康     | 启动后                                            | 无 backend-down/offline 横幅，composer 可用  |
| F16 | 重命名会话   | 铅笔 → 输入 → Enter                               | 行标题更新（amber renamed 态）               |
| F17 | 会话过滤     | 点 Filter 展开/收起                               | 过滤区切换                                   |
| F18 | 批量删除     | 勾选行 → Delete Conversations → Delete            | 行消失                                       |
| F19 | 会话右键菜单 | 右键行                                            | context menu 出现                            |
| F20 | 侧栏折叠     | 工具栏折叠按钮                                    | 标题行消失/出现                              |
| F21 | 项目文件树   | 绑定目录（API fixture）→ 文件 body                | 树列出 fixture 文件                          |
| F22 | 复制消息     | AI 行 Copy 按钮                                   | 交互不崩（剪贴板权限视 WebView）             |
| F23 | 会话内查找   | Ctrl+Shift+F → 输入 → 关闭                        | 计数 1/、环高亮                              |
| F24 | 命令面板     | Ctrl+K → 输入                                     | results/recent 态                            |
| F25 | 回合折叠     | 工具回合结束后展开                                | 折叠条 + read_file 卡片                      |
| F26 | 推理块       | `[e2e:reasoning]`                                 | 回合存活（openai 透传有限，DeepSeek 全渲染） |
| F27 | token 计量   | 回合结束                                          | 气泡下 ≈N tokens                             |
| F28 | 编辑消息     | 铅笔 → banner → cancel                            | banner 出现并消失                            |
| F29 | 分叉回合     | AI 行 Fork                                        | 导航到新会话                                 |
| F30 | 排队消息     | 流式中发第二条                                    | queue 提示 + 停止后 idle                     |
| F31 | 多轮持久     | 两个回合                                          | 早前文本仍在                                 |
| F32 | 技能触发     | composer 输入 `$`                                 | composer 存活 + 面板可达                     |
| F33 | 命令触发     | composer 输入 `/`                                 | 同上                                         |
| F34 | 文件提及     | composer 输入 `@`                                 | 同上                                         |
| F35 | 访问模式     | 盾牌 → 三档切换                                   | 选中态更新（popover 正确关闭）               |
| F36 | 语音按钮     | 点麦克风                                          | 无权重时优雅降级不崩                         |
| F37 | 通知面板     | 铃铛                                              | notification panel 渲染                      |
| F38 | 会话预设视图 | preset 触发器                                     | 面板渲染                                     |
| F39 | 日志面板     | Menu → Logs                                       | 面板渲染                                     |
| F40 | 统计面板     | Menu → Statistics                                 | 面板渲染                                     |
| F41 | 知识图谱     | Menu → Knowledge Graph                            | 面板渲染                                     |
| F42 | 记忆面板     | Menu → Memory                                     | 面板渲染                                     |
| F43 | 技能管理     | Menu → Skills                                     | 面板渲染                                     |
| F44 | 心跳面板     | Menu → Heartbeat                                  | 面板渲染                                     |
| F45 | 定时任务     | Menu → Cron Tasks                                 | 面板渲染                                     |
| F46 | 扩展面板     | Menu → Extend                                     | 面板渲染                                     |
| F47 | 搜索后端     | Menu → Web Search                                 | 面板渲染                                     |
| F48 | 人设编辑     | Menu → Presets & Role                             | Character Setup 渲染                         |
| F49 | 账户面板     | Menu → Account                                    | auth-status 渲染                             |
| F51 | 语言切换     | System Config → Language                          | 中文/English 切换并回退                      |
| F52 | 快捷键设置   | System Config → Shortcuts                         | 快捷键行渲染                                 |
| F53 | 直播安全     | System Config → Live Safety                       | 安全开关渲染                                 |
| F54 | 应用配置     | System Config → App Config                        | sherry-entry-* 渲染                          |
| F55 | 模型配置     | System Config → Model Config                      | 组卡片渲染（code-embedder 标记）             |
| F56 | 操作终端     | Toolbox → Terminal → echo                         | 输出出现                                     |
| F57 | 白板         | Toolbox → Whiteboard → 画                         | canvas 可绘                                  |
| F58 | 浏览器面板   | Toolbox → Browser                                 | 面板渲染                                     |
| F59 | 任务气泡     | composer 任务入口                                 | 交互不崩                                     |
| F60 | 上下文环     | 环按钮 → 面板/预算                                | legend/open-budget                           |
| F61 | 单机无切换   | —                                                 | TenantSwitcher 隐藏                          |
| F62 | 桌面标题栏   | —                                                 | TitleBar 渲染                                |
| F63 | Git 图       | Files body → Shadow G…                            | checkpoint/分支态渲染                        |

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
