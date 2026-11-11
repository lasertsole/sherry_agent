# 🧠 本地模型 Daemon：Embed、Reranker 与本地 Chat

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本地 GGUF 模型（bge-m3 向量、bge-reranker、辅助聊天模型）不再在主进程内加载。每个模型族由一个独立子进程承载：权重只加载一次、通过 loopback HTTP JSON 应答、空闲一段时间后自行退出。主进程只保留请求/响应载荷。

**事实来源：** `models/daemon/base.py`、`models/embed_model/text/daemon.py`、`models/embed_model/text/core.py`、`models/reranker_model/daemon.py`、`models/reranker_model/__init__.py`、`models/LLMs/local_daemon/server.py`、`models/LLMs/local_daemon/client.py`、`models/LLMs/auxiliary_llm/core.py`、`config/features/infra_side/local_daemons.py`、`packaging/backend_entry.py`、`skills/builtin/core/speech_to_text/scripts/core.py`。以下每条结论均已对照代码与「实测基线」中的数据核对。

## 🎯 为什么用 daemon

后端是**单个** Python 进程（`server/__main__.py` 固定 `app.config.processes = 1`）：所有会话、中间件链、WebSocket 推送与工具沙箱都在这里。把 600 MB 的向量模型或 5.5 GB 的聊天模型放进这个进程，等于让最需要保持响应的进程承担最容易被 OOM 杀掉的成本。

旧的在进程内 embed 路径实测问题：

| 指标（bge-m3 q8_0，本机） | 进程内（之前） | Daemon（之后） |
| :--- | :--- | :--- |
| 每次 embed 的热启动 `Llama(...)` | 约 1.27–1.30 s | 0（已常驻） |
| 每批推理 | 约 0.32 s | 约 0.49 s（含 loopback HTTP） |
| 调用期间主进程 RSS | 峰值约 1017–1055 MB（`ru_maxrss`） | 46.5 → 47.3 MB（`VmRSS`） |
| 模型 RSS | 在主进程内 | 约 1045 MB（daemon 内） |
| 与进程内向量的 cosine | — | 1.0（完全一致） |

**这是内存隔离，不是总量减少。** 所有模型族同时活跃时，总内存（主进程 + daemon）基本持平甚至略高。收益是：主进程永远不出现大峰值；关闭的功能不 spawn、零内存；热调用更快，因为 GGUF 不再每次重新映射。

## 🏗️ 架构

```text
主进程（编排）                                   daemon 子进程（detached）
  _LocalEmbeddingBackend.embed(texts)  ─HTTP──▶  POST /embeddings
  _LocalGgufBackend.rank(...)          ─HTTP──▶  POST /rerank | /filter | /predict
  DaemonChatClient.create_chat_...     ─HTTP──▶  POST /v1/chat/completions
                                                 一份 GGUF，只加载一次
                                                 推理由锁串行化
                                                 idle watchdog 到点退出进程
```

客户端半边是共享的。`DaemonSpec` 描述端点、pid 文件与 import 目标；`models/daemon/base.py` 实现唯一一套生命周期：

- **就绪判定严格。** `/healthy` 必须返回 `{"ready": true}`。进程活着但模型仍在加载，不算就绪。
- **spawn 去重。** 记录的 pid 仍存活时不重复 spawn；陈旧 pid 文件会被替换。
- **ReadyState 三态。** `READY`、`WARMING`（进程存在，继续等）、`UNAVAILABLE`（没有进程且起不来）。正是这个区分让客户端能安全回退，而不会同时再加载一份权重。
- **kill** 先 SIGTERM，等最多 5 秒，再 SIGKILL，并回收自己的子进程。只有真正 spawn 了 daemon 的进程才注册 atexit kill。
- **空闲退出** 由 `IdleWatchdog` 负责：`*_idle_timeout_s` 内没有请求就 `os._exit(0)`，下次调用再冷启动。

### 端点、端口与 pid 文件

| 模型族 | 端口 | pid 文件 | 端点 |
| :--- | :--- | :--- | :--- |
| Embed | 9012 | `.embed_daemon.pid` | `GET /healthy`、`POST /embeddings` |
| Reranker | 9013 | `.reranker_daemon.pid` | `GET /healthy`、`POST /rerank`、`/filter`、`/predict` |
| 本地 Chat | 9014 | `.llm_daemon.pid` | `GET /healthy`、`POST /v1/chat/completions` |
| 视觉 Chat | 9015 | `.vision_daemon.pid` | `GET /healthy`、`POST /v1/chat/completions`（mmproj） |
| STT（已重构） | 9011 | `.stt_daemon.pid` | `GET /healthy`、`POST /transcribe` |

pid 文件与 daemon 日志位于 `APP_HOME`（打包态是可写的应用数据目录，源码检出时就是仓库根），绝不写只读代码树。所有 daemon 只绑定 `127.0.0.1`，JSON、无鉴权，与既有 STT daemon 一致——它是本地缓存，不是网络服务。

### 打包

源码检出用 `python -c "from <module> import main; main()"` 启动。PyInstaller 冻结态无法执行 `-c`，因此用 `--sherry-daemon <module>` 重新执行打包后的后端；`packaging/backend_entry.py` 在导入 `server.__main__` **之前** 分发到该模块的 `main()`，所以 daemon 子进程不会初始化日志、`nest_asyncio` 或 LangSmith。`--sherry-home` 与 `--sherry-desktop` 会一并传递，子进程解析到同一个可写 home。

## 🔌 三个客户端

**Embedding**（`models/embed_model/text/core.py`）。daemon 返回原始向量，客户端保留 L2 归一化与顺序校验。它发送调用点原本就已分好的批（`memory_embeddings` 每批 32 条、消息索引器每批 64 条），索引链路不需要第二层分批。daemon 报错后重试一次（覆盖空闲退出或崩溃）；第二次失败交给调用方（语义召回本身 fail-open）。

**Reranker**（`models/reranker_model/__init__.py`）。`rank`、`filter`、`predict` 各对应一个端点；`predict_scores` 一次请求发送整批 pair，而不是每对一次往返。`RERANKER_MODEL_LOCAL=false` 时云端后端完全不变。

**本地 Chat 与视觉**（`models/LLMs/auxiliary_llm/core.py` / `models/LLMs/base_local_llama.py`）。本地 GGUF 路径**不用**原生 tool call：`LocalToolBinder` 注入 JSON 指令，`LocalStructuredOutput` 用 instructor 包装 `create_chat_completion_openai_v1`。因此 daemon 暴露的是 llama-cpp 自己的 `create_chat_completion` 形状，`models/LLMs/local_daemon/client.py` 只是实现这两个方法的小型 shim。`create_chat_completion_openai_v1` 把响应校验成 OpenAI `ChatCompletion`（补上 instructor 需要的 id/object/created/model 字段），且不会把 instructor 的控制参数发过线。daemon 起不来或超时仍未就绪时，`_LocalAuxiliaryChatModel` 回退到进程内 `Llama`——功能优先，隔离其次。多模态基类复用同一个客户端访问 9015 daemon；`image_url` 内容块原样传给 llama-cpp 的 Qwen2.5-VL handler。

## 🛟 失败行为与开关

| 情况 | 行为 |
| :--- | :--- |
| 两次调用之间 daemon 空闲退出 | 客户端重试一次，`ensure_ready` 重新 spawn |
| daemon 仍在预热（模型加载中） | `WARMING`；embed/reranker 抛可重试错误；本地 chat 回退进程内 |
| 完全无法启动 daemon | `UNAVAILABLE`；embed/reranker 在本进程剩余生命周期内使用旧的进程内加载；本地/视觉 chat 用进程内 `Llama` |
| daemon 请求中途崩溃 | 本次请求失败；记忆召回保持 cosine 顺序，索引构建记录错误 |
| 关闭功能 | `EMBEDDING_DAEMON_ENABLED=false`、`RERANKER_DAEMON_ENABLED=false`、`AUXILIARY_LLM_DAEMON_ENABLED=false` 恢复旧路径，完全不 spawn |

所有设置都在 `LOCAL_DAEMONS`（`config/features/infra_side/local_daemons.py`）中，并带环境变量覆盖：端口（`*_DAEMON_PORT`）、就绪预算（`*_READY_WAIT_S`）、空闲窗口（`*_DAEMON_IDLE_TIMEOUT_S`）、请求超时（`*_HTTP_TIMEOUT_S`）、`AUXILIARY_LLM_N_CTX` 以及 `VISION_LLM_DAEMON_*`。`auxiliary_n_ctx` 默认是历史值 40 960；调低时本地 `max_tokens` 会被压到窗口的一半，避免小 KV cache 搭配超长补全请求。

## 📏 实测基线（2026-11-11，本机）

| 测量项 | 结果 |
| :--- | :--- |
| `import server.__main__` | 峰值 RSS 约 26.7 MB，约 0.86 s |
| `agent.core` + `server.trigger.http` | 峰值 RSS 约 153.9 MB，约 5.2 s |
| 启动 import 集合 | `torch`、`transformers`、`cv2`、`mineru_vl_utils`、`leidenalg`、`snkv`、`tree_sitter`、`gguf`、`llama_cpp` 均不在 |
| Embed daemon 首次调用（spawn + 加载） | 3.8 s |
| Embed 热调用 | 0.49 s（进程内约 2.88 s） |
| embed 期间主进程 RSS | 46.5 → 47.3 MB |
| daemon RSS | 1045.6 MB |
| 向量 cosine（daemon vs 进程内） | 1.0 |
| Reranker daemon 首次调用（spawn + 606 MB GGUF 加载） | 32.8 s |
| Reranker 热排序（4 条文档） | 1.33 s（进程内每次约 31.5 s，含重新加载） |
| rerank 期间主进程 RSS | 46.6 → 46.9 MB；daemon 898.4 MB |
| Reranker 分数/顺序（daemon vs 进程内） | 完全一致 |
| 本地 Chat 首次调用（Qwen2.5-0.5B-Instruct Q4，spawn + 加载） | 3.33 s |
| 本地 Chat 热调用 | 0.77 s |
| 本地 Chat 期间主进程 RSS | 65.3 → 65.9 MB；daemon 600.4 MB |
| 本地 Chat 工具调用 + 结构化输出 | 解析出 `add(a=2,b=2)`；`Answer(answer='Paris')` |
| 同一模型在进程内加载 | 主进程峰值 +537.9 MB |
| 视觉首次调用（Qwen2.5-VL-3B Q4 + mmproj，96 px 图片） | 13.61 s（CPU：图片编码 + prompt） |
| 视觉热调用 | 7.44 s |
| 视觉调用期间主进程 RSS | 67.3 → 20.2 MB（峰值 68.2）；daemon 3 571 MB |
| 视觉回答 | `Red`（正确），走 9015 端口 |

**「重依赖懒加载」方向按其实测 Kill criteria 跳过：** 完整启动 import 链（模块引导 + agent core + curator/cron + trigger + services）没有加载 `torch` / `transformers` / `cv2` / `mineru_vl_utils` / `leidenalg` / `snkv` / `tree_sitter` / `gguf` / `llama_cpp` 中的任何一个。这些依赖本身并不小——单独 import 的代价约为 `torch` 490 MB / 5.9 s、`transformers` 41 MB / 9.9 s，其余在基础环境中根本未安装——因此它们已经处在优化想要的位置：各自的功能函数内部。守护测试 `tests/perf/test_boot_import_set.py` 会在任何一个重新进入启动集合时失败。剩余启动成本来自 `openai` / `langchain` / `langgraph` 与日志/配置引导，这些每条路由都要用。（`PIL` 通过媒体压缩模块在启动时被导入；实测约 49 ms 且 RSS 峰值无可测增量，不值得为此改代码。）

**视觉同构。** ITTT/VTTT 走**同一个**本地 chat daemon（端口 9015）：daemon 读取 `SHERRY_LOCAL_LLM_MMPROJ_PATH` 并追加 `Qwen25VLChatHandler`，`n_batch = n_ubatch = n_ctx`，与进程内 `LocalMultimodalLlamaChatBase` 完全一致。两个模型族解析到同一套 Qwen2.5-VL 权重 + mmproj，因此共享一个视觉 daemon；第二个不同的视觉模型需要单独端口。`LocalMultimodalLlamaChatBase` 优先尝试 daemon，起不来时回退进程内客户端。安全审核仍复用辅助客户端；审核请求排在长生成之后时可能触及 20 秒 fail-open 预算，与今天的结果相同。

## 🧪 测试

- `tests/models/daemon/test_base.py` —— pid 文件、就绪三态、spawn 命令（源码 vs 冻结）、atexit、kill/回收、请求解码、空闲 watchdog。
- `tests/models/embed_model/text/test_embed_daemon.py` —— 真实 loopback server + 假模型：顺序、分批、校验、健康检查；客户端：载荷、重试、预热、回退、归一化。
- `tests/models/reranker_model/test_daemon.py` —— rank/filter/predict 线形状、校验、客户端重试与回退。
- `tests/models/LLMs/local_daemon/test_local_daemon.py` —— llama-cpp 参数转发、instructor 兼容的 `ChatCompletion`、辅助模型 daemon/回退接线、`n_ctx`/`max_tokens` 钳制、视觉 daemon（mmproj handler、端口 9015）及其回退。
- `tests/packaging/test_backend_entry.py` —— `--sherry-daemon` 解析与分发。
- `tests/skills/builtin/core/speech_to_text/test_stt_daemon_pid.py` —— STT 重构后在共享生命周期上保持同一 pid 契约。
- `tests/config/test_features_infra_side.py` —— `LOCAL_DAEMONS` 默认值、环境覆盖、非法值回退。

## ⚠️ 限制

- 每个端口一个模型实例。两个不同的辅助 GGUF（第二个本地 profile）不能共享 daemon；路径在 spawn 时解析，daemon 只服务它加载的第一个。
- 没有请求队列公平性：`ThreadingHTTPServer` + 一把推理锁，等待线程的顺序由 OS 调度决定。长批次会推迟一次召回，对本地缓存而言可以接受。
- 无鉴权、无 TLS、不允许跨主机。daemon 与 STT 一样按构造只监听 `127.0.0.1`。
- 本地 chat daemon 不支持流式。本地辅助路径从不流式（调用点全是 `invoke`/`ainvoke`），因此 `stream: true` 会被明确拒绝，而不是悄悄降级。
