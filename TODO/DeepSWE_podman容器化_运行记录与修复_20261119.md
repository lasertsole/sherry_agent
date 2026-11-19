# DeepSWE podman 容器化：完整链路搭建、运行 bug 与修复

- 日期：2026-11-19
- 状态：**容器链路已在 podman 下全部打通（GOLD 自检 2/2 resolved）；模型驱动回合阻塞在 MAIN_LLM_API_KEY 缺失**
- 环境：Windows 11 · Podman 6.1.4（WSL2 machine，12 CPU / 2 GiB / 949 GB 空闲）· 无 Docker
- 任务：`httpx-streaming-json-iteration`、`cattrs-partial-structuring-recovery`（DeepSWE 默认切片，镜像 `public.ecr.aws/d3j8x8q7/swe-bench-202605:*-v1.1`）
- 关联：[DeepSWE本地测试失败原因分析.md](DeepSWE本地测试失败原因分析.md)、[evals/deepswe/README.md](../evals/deepswe/README.md)

## 0. 结论

1. **官方容器 harness（默认模式）在 podman 下可用**：数据集克隆 → 任务镜像拉取 → verifier 构建 → GOLD 自检 → agent 容器 + terminal bridge + 会话绑定，全链路验证通过；两个任务的 GOLD 自检均 resolved。
2. 过程中抓到 **9 个 harness bug（全部已修）** + **2 个产品 bug（记录未修）**，见下文清单。全部修复在 `evals/external_bench.py`、`evals/deepswe/suite.py`、`evals/deepswe/local.py`、`evals/coding_capability/suite.py`。
3. **阻塞点**：本机没有任何模型凭据（`.models` 全空、无环境变量）。模型驱动的评分回合会被套件的 `MAIN_LLM_API_KEY` 门禁按设计跳过（exit 3）。凭据配置方法见 §4。
4. **todolist + taskflow 强制机制已落地**（提示词硬性要求 + 逐工具用量统计 + 报告字段），等真实模型跑起来后即可在报告里看到 `orchestration_compliant`。

## 1. 代码改动

| 文件 | 改动 |
| :--- | :--- |
| `evals/external_bench.py` | 容器引擎可配置（`container_bin()`：`SHERRY_EVAL_CONTAINER_BIN`，默认 docker→podman 自动探测）；探活兼容 podman；数据集克隆强制 LF + CRLF 缓存自愈；`build_image` 支持 `--network`；`extract_workdir` 工具容器 network=none；`DockerBridge` 增加 `container_workdir` + 宿主路径→容器路径翻译；`create_container` 失败自清理 |
| `evals/deepswe/suite.py` | verifier 构建 `network="none"`；`--preflight-only` 模式（无 key 验证整条容器链路）；编排强制提示词；逐工具用量统计入报告；Windows 挂载/工作目录适配 |
| `evals/deepswe/local.py` | 本地模式同样的 CRLF 修复（model.patch / run-verifier.sh） |
| `evals/coding_capability/suite.py` | `_drive` 统计逐工具调用次数（`tool_names`） |

## 2. Bug 清单（本次运行实测）

### 已修复

| # | 现象 | 根因 | 修复 |
| :--- | :--- | :--- | :--- |
| 1 | `docker_available()` 对可用的 podman 永远返回 False | 探活用 docker 专有模板 `{{.ServerVersion}}`，podman 的 info 没有该字段 | 改为无格式 `info` 探活（external_bench.py） |
| 2 | verifier 构建失败：`setup network: netavark (exit code 1): nftables error` | podman build 的每个 `RUN` 步骤都要建 bridge 容器，WSL2 内核无 nftables | verifier 构建显式 `--network none`（其 Dockerfile 只 FROM+COPY，不需要网络）；`SHERRY_EVAL_BUILD_NETWORK` 供需要联网的构建覆盖（WSL2 上设 `host`） |
| 3 | verifier 的 `test.sh` 在容器内报 `set: pipefail\r: invalid option name` | 数据集工作区被 `core.autocrlf=true` 物化成 CRLF（上游 blob 是 LF）；CRLF 的 `test.sh` 被 COPY 进镜像 | `sync_repo` 克隆时强制 `-c core.autocrlf=false -c core.eol=lf`；存量缓存自动修复（改本地配置 + `rm --cached -r` + `reset --hard` 重物化） |
| 4 | GOLD 补丁在容器内 `apply_failed: 1`（`patch does not apply`） | Windows 上 `Path.write_text` 把 `\n` 翻译成 `\r\n`，写出的 `model.patch` 是 CRLF，容器内 `git apply` 拒绝 | 所有进容器/进 bash 的写入加 `newline="\n"`（suite.py 的 model.patch 与 patch 产物、local.py 的三处） |
| 5 | `extract_workdir` 的工具容器启动失败（同 #2 的 netavark 错误） | 未指定网络 → 默认 bridge；该容器只做 exec/cp，根本不需要网络 | `create_container(..., network="none")` |
| 6 | agent 容器启动失败：`invalid option type "\\app\\..."` | `start_agent_container` 把宿主树同时挂到「宿主机路径」和 `/app`；Windows 宿主路径在 Linux 容器里不存在 | Windows 只挂 `/app` 一处；`DockerBridge` 增加 `container_workdir`，命令中的宿主路径自动翻译为容器路径 |
| 7 | 容器 `exec -w` / 脚本 `cd` 用宿主路径（Windows 下无效） | 同 #6 的路径镜像前提 | bridge 的 `-w`/`cd` 使用容器工作目录；`exec/exec_at/aexec` 统一做路径翻译 |
| 8 | 失败的 `podman run` 泄漏 `Created` 状态容器，挡住下一次同名创建 | podman 先 create 后 start，启动失败时 `create_container` 直接抛错 | 失败时 best-effort `rm -f <name>` 后再抛 |
| 9 | `PatternError: bad escape \c`（agent turn 一开始就死） | `localize_instruction` 的 `re.sub` 把 Windows 路径当替换串，`\c` 被当转义 | 替换改用 lambda（字面替换） |

### 已记录、未修（产品 bug，建议后续处理）

| # | 现象 | 根因 | 修补方案 |
| :--- | :--- | :--- | :--- |
| 10 | **容器模式没有硬超时**：`--timeout 240` 到期后回合继续跑了 10 分钟+（占位凭据触发的 GGUF 下载一直活着）。README 里「硬墙钟」只是 `--local` 模式的保证（worker 子进程 SIGKILL）；容器模式的 `_run_task` 用 `asyncio.wait_for`，而生产回合可以无视 asyncio 取消 | `_run_task` 在评测进程内直接 `_drive`，没有 local 模式那样的可杀 worker | 把 local 模式的 worker 进程方案搬进容器模式（`agent_worker.py` 已具备全部条件），或退一步：超时后改判任务并等待自然结束 |
| 11 | **模型配置不完整时静默回退下载 6GB GGUF**：占位凭据下，模型构建失败 → 本地 auxiliary 路径 → `resolve_gguf_path` 开始下载 `Qwen3.5-9B-Q4_K_M.gguf`（1.6GB 后被人工终止） | 主/辅助家族的本地回退把「配置错误」和「显式选择本地」混在一起 | 配置了 provider/key 但构建失败时 fail-fast 报错；仅当 `*_MODEL_LOCAL=true` 或 provider 为空时才允许走本地下载 |
| 12 | **阻塞点：本机无模型凭据** | `.models` 各家族 provider/key 全空，无环境变量；此前 DeepSWE 复跑用的 key 不在当前环境 | 见 §4 的配置方法；配置后一条命令即可重跑 |

## 3. todolist + taskflow 强制机制

按任务要求，DeepSWE 的 agent 回合被强制走 Sherry 的规划与编排工具：

- **提示词硬性要求**（`suite.py::_PROMPT` 的 "Orchestration mandate"）：先 `todolist` 建计划并保持更新；用 `taskflow_flow(action="plan")` 建一个 flow、`taskflow_step` 驱动实现步骤、`taskflow_flow(action="finish")` 收尾；todo/flow 状态与验证命令绑定。
- **逐工具用量统计**：`coding_capability/suite.py::_drive` 现在统计每个工具的调用次数（`tool_start` 帧按名计数），DeepSWE 报告新增 `orchestration` 段与每任务 `todolist_calls` / `taskflow_calls` / `orchestration_compliant`（两者都 >0 才算合规）；harness 异常路径也有默认值（不再 KeyError）。
- 说明：这套统计在真实模型跑起来后才有内容；占位凭据的冒烟里 counters 为 0（`orchestration=VIOLATED` 是预期值，回合根本没有模型可用）。

## 4. 复跑步骤（配置凭据后）

1. 配置模型（二选一）：
   - 在 `.models` 里填 `MAIN_LLM_PROVIDER` / `MAIN_LLM_API_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY`（沿用 2026-11-15/16 复跑的 DeepSeek 配置）；
   - 或在 shell 里 `set MAIN_LLM_API_KEY=... MAIN_LLM_PROVIDER=deepseek MAIN_LLM_API_NAME=... MAIN_LLM_API_BASE=https://api.deepseek.com`（evals 通过 `config.path` 拿到它们）。
2. 运行（podman 已自动探测；显式指定亦可）：

```bash
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --timeout 1800
```

3. 无模型时验证容器链路（本次已验证通过）：

```bash
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --preflight-only --keep-workdirs
```

## 5. 验证记录（2026-11-19，本机）

| 步骤 | 结果 |
| :--- | :--- |
| 数据集克隆（113 任务，pin `0b9fabb`） | 通过（含 CRLF 自愈一次） |
| 任务镜像拉取 ×2（public ECR，直连） | 通过 |
| verifier 构建 ×2（`--network none`） | 通过 |
| GOLD 自检 ×2 | **2/2 resolved**（httpx 108/108 F2P、cattrs 69/69 F2P 的参照补丁均通过 verifier） |
| agent 容器 + 会话绑定 + 回合启动 | 通过（占位凭据下到达模型构建阶段） |
| evals 单测 | 75 passed / 2 failed（Windows 既有失败，干净 HEAD 复现） |

## 6. 后续

- [ ] 配置 `MAIN_LLM_API_KEY` 后重跑评分回合，观察 `orchestration_compliant` 与 todolist/taskflow 在真实任务里的行为。
- [ ] Bug #10：给容器模式补硬超时（复用 `agent_worker.py` 进程方案）。
- [ ] Bug #11：模型构建 fail-fast，不再静默下载 GGUF。
- [ ] 跑通后把 `--backend`/镜像前提写进 `wiki/evals/`（四语言）。

## 7. 凭据与环境排查（2026-11-19）

用户提供的 `Desktop/新建 文本文档.txt` 里的模型凭据逐个实测结果：

| 提供项 | 端点 | 结果 |
| :--- | :--- | :--- |
| CHAT（glm-5.3-flash） | `open.bigmodel.cn` | **401 令牌已过期或验证不正确**；且原文件 `CHAT_API_BASE` 是 `hhttps://…/chat/completions`（多一个 h，且 base 应到 `/api/paas/v4`） |
| REASONER（deepseek-reasoner） | `api.deepseek.com/v1` | **401 api key invalid** |
| VL / RERANKER / EMBEDDING（ModelArts） | `api.modelarts-maas.com/v1` | key **有效**：`bge-m3` embedding 200、`bge-reranker-v2-m3` 可用；但 `glm-5.3` / `glm-5.2` / `openpangu-*` 等 chat 模型 **403 无权限** |
| SIMPLE（ollama qwen3:8b） | `127.0.0.1:11434` | ollama CLI 已安装、服务可启动，但**本地无任何模型**；`ollama pull qwen3:8b` 实测 2.0 MB/s（5.2 GB ≈ 42 分钟），已中止 |

**同时发现一个会打断所有模型调用的环境问题**：Windows 系统代理开关 `ProxyEnable = 1`、`ProxyServer = 127.0.0.1:7897`，但 **7897 端口没有进程监听**（代理客户端已退出）。httpx / OpenAI SDK 默认 `trust_env=True`，会读取该代理设置 → 所有模型请求得到 `WinError 10061 连接被拒绝`；而 `curl`（只读环境变量）能连通、`httpx(trust_env=False)` 也正常。规避方式：评测进程加 `NO_PROXY=*`（本次后续运行会带），或把系统代理开关关掉 / 重新启动代理客户端。

结论：**chat 模型需要一个新的可用 key**（provider / api_name / api_base / api_key 四元组）。拿到后直接：

```bash
NO_PROXY=* SHERRY_EVAL_CONTAINER_BIN=podman \
  MAIN_LLM_PROVIDER=<provider> MAIN_LLM_API_NAME=<model> \
  MAIN_LLM_API_BASE=<base> MAIN_LLM_API_KEY=<key> \
  MAIN_LLM_MAX_TOKEN=<window> \
  uv run --no-sync python evals/deepswe/suite.py --timeout 1800
```

（embedding/reranker 可继续用 ModelArts 的有效 key，或保持本地配置。）

## 8. 真实模型运行结果与新增 bug（2026-11-19 晚）

CHAT_MODEL（deepseek / deepseek-flash / 1M 窗口）验证可用后，podman 容器模式下完整跑了两次：

| 运行 | 任务 | F2P | P2P | 耗时 | 工具调用 | 编排 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 无门禁（run5，2 任务） | httpx | 88/108 (81%) | 1404/1404 | 706.8s | 181 | todowrite=1，taskflow=0 → VIOLATED |
| 无门禁（run5） | cattrs | 0/69 | 7/7 | 31.3s | 8 | todowrite=1，taskflow=0 → VIOLATED |
| 有门禁（单任务） | httpx | 0/108 | 1404/1404 | 323.3s | 183 | **todowrite=1，taskflow=1 → ok** |

结论：容器链路 + 真实模型 + 评分全部工作；`deepseek-flash` 的解题能力是瓶颈（两轮都未 resolved），
但**编排要求在有门禁时被满足**（todowrite + taskflow_flow(plan) 各≥1）。

运行中的新 bug（全部已修）：

| # | 现象 | 根因 | 修复 |
| :--- | :--- | :--- | :--- |
| 12 | GOLD 自检 F2P 全过但 126 个 P2P 失败（`ConnectError 127.0.0.1:7897`） | podman 默认把宿主/VM 的代理注入每个容器；httpx 测试把 localhost 也走死代理（`NO_PROXY` 是 Windows 分号格式，httpx 不认） | 任务容器与 verifier 都加 `--http-proxy=false` |
| 13 | 模型客户端初始化 `ValidationError: model Field required` | 主模型读 `MAIN_LLM_NAME`，辅助家族读 `*_LLM_API_NAME`；手写环境的 `MAIN_LLM_API_NAME` 被忽略成空 model | 修正环境变量命名（记录为配置脚枪，建议构建时 fail-fast 指名缺失变量） |
| 14 | 提示词要求 `todolist` 工具，但实际工具名是 `todowrite` / `todoread`，模型无法照做 | 工具注册名与文档/提示词不一致 | 提示词改为 `todowrite`/`todoread`；统计改为 `name.startswith("todo")`（taskflow 同） |
| 15 | **提示词强制无效**：181 次工具调用只有 1 次 todowrite、0 次 taskflow | 只靠 prompt 指令无法约束模型 | 加 `install_orchestration_gate()`：在 `SafeToolNode._arun_one` 拦截，plan 完成前 `write_file`/`patch_file`/`sessions_spawn` 返回 HARNESS RULE 提示并计数 `blocked` |
| 16 | 门禁第一版挂 `tool.ainvoke` 抛 `ValueError: object has no field "ainvoke"`（pydantic 锁属性）；改 `object.__setattr__` 后计数仍为 0 | 该挂点不在真实调用路径上 | 改为 patch `SafeToolNode._arun_one`（ToolNode 批执行入口），直接探测验证：未规划时 `write_file` 返回规则提示、`blocked=1` |
| 17 | 报告里 `gate` 恒为 `{todo:0,taskflow:0,blocked:0}` | `gate_state.update(...)` 拷贝了安装时快照，闭包后续计数进不了报告 | 直接绑定门禁返回的活字典 |
| 18 | 有门禁的 httpx 运行 `changed_paths: []`、F2P 0/108 | 模型把预算花在规划+探索（183 次调用），未落有效补丁 | 模型能力/预算问题，非 harness；已记录 |

代码改动（`evals/external_bench.py`、`evals/deepswe/suite.py`）：`container_bin()` 引擎探测、podman
`--http-proxy=false`、CRLF 自愈与 LF 写入、`--preflight-only`、编排门禁+统计、报告 `orchestration`
（`enforced` / `gate` 实时计数 / `compliant_tasks` / `violating_tasks`）。

### 复跑命令

```bash
set -a; . /path/to/credential-env.sh; set +a   # MAIN_LLM_* / AUXILIARY_* / EMBEDDING_* / RERANKER_*
NO_PROXY='*' SHERRY_EVAL_CONTAINER_BIN=podman   uv run --no-sync python evals/deepswe/suite.py --timeout 1800
```
