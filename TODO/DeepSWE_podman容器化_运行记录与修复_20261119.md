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

## 9. deepseek-reasoner 运行（2026-11-19 22:00）与 Bug #19–#21

配置：`MAIN_LLM_NAME=deepseek-reasoner`（CHAT key 同样有效），单任务 httpx，门禁开启。

结果：**未跑完**（人工在约 7 分钟时终止）。没有产出补丁，卡在三个产品 bug 的叠加回路里：

| # | 现象 | 证据 | 修补方案 |
| :--- | :--- | :--- | :--- |
| 19 | **熔断器“halt”不终止回合**：它返回 `🔴 Agent halted: tool [todowrite] triggered circuit breaker … entire turn is being terminated`，但回合继续，模型把这句话当工具结果继续重试 | MesMemory 里 190 条工具结果中 **161 条是熔断 halt 消息**；日志中 `todowrite` 被调用 158 次（约 5 分钟） | 熔断触发必须真正结束当前回合（设置终止状态并让 stream 发 `done`/`stopped`，而不是返回一条可被模型消化的 ToolMessage）；同时给“同一工具连续 N 次相同调用”加硬上限 |
| 20 | **TaskFlow 步骤状态死胡同**：auto-dispatch 把 step-1 置为 dispatched，随后它变成 skipped；模型尝试 `status=done` 被两次拒绝（`cannot become status=done; use dispatch/resume`），没有可用的恢复路径，于是开始重复调用 | 工具结果：`step 'step-1' is dispatched and cannot be downgraded to status=ready`、`step 'step-1' (status=skipped) cannot become status=done` | 为 skipped/failed 步骤提供幂等的恢复动作（例如 `taskflow_step(action="retry")` 或允许 done 覆盖 skipped 并说明语义），并让错误文案直接给出可用调用 |
| 21 | **/app 路径幻觉**：模型第一次写文件用 `C:pp\httpx\_models.py`（容器路径与 Windows 盘符拼接），被 PathGuard 以 “External file access denied” 拒绝后没有纠正 | 工具结果 id 13：`External file access denied: C:pp\httpx\_models.py` | 在 prompt 中明确“项目根目录是 <host path>，不要使用 /app 或 C:pp”；或在 Windows 本地化时把 `/app/...` 一并替换为宿主路径（当前只替换独立 `/app` 前缀，`/app/httpx` 这类拼接可能漏改） |

注：第 14 条（todowrite 工具名）修复后，deepseek-flash 与 reasoner 都能正确调用规划工具；本次 reasoner 的失败是状态机/熔断语义问题，不是编排门禁本身。

## 10. 修复 #19–#21 后的复跑（2026-11-19 22:15，deepseek-reasoner）

**修复内容**（提交 `15beb5b0`）：

| Bug | 修复 |
| :--- | :--- |
| #19 熔断不终止回合 | `ToolGuardrails` 增加 `wrap_model_call`/`awrap_model_call`：`halt_decision` 已置位时直接返回终止 `AIMessage`（不再调用模型）。新增 2 个单测 |
| #20 skipped 步骤死胡同 | `step_state` 允许 `skipped → ready` 显式重开；`update_steps` 接受该重开，并把报错改为「skipped 用 status=ready 重开 / dispatched 用 resume / done 不可变」。新增 2 个单测 |
| #21 /app 路径幻觉 | DeepSWE 提示词显式给出唯一项目根目录，并禁止 `/app`、`C:pp` 及根外路径 |

**复跑结果**（httpx，门禁开启，timeout 1800）：

| 指标 | 数值 |
| :--- | :--- |
| F2P | **99/108（91.7%）** |
| P2P | 1404/1404 |
| 耗时 / 工具调用 | 782.0s / 55 |
| 编排 | `todolist=6，taskflow=1，orchestration=ok` |
| resolved | ✗（需 108/108） |

对比修复前同一模型（reasoner）那次：卡在 161 条熔断消息、无补丁；现在正常完成 55 次调用、产出补丁、P2P 全过。
剩余 9 个失败全部是同一参数化用例 `test_iter_json_rejects_non_json_media_types`：实现没有对非 JSON
media type 抛 `DecodingError` —— 隐藏测试里的一支行为，模型没实现（不是 harness 问题）。

## 11. 追问：todolist 的强制自检为什么没生效（Bug #22）

结论：**自检插入了，但没有被执行完**；根因是评测环境没有「自动续跑」的触发器。

证据（run `20261119_221455`，F2P 99/108 那次）的 todo 表：

| # | 状态 | kind | 内容 |
| :--- | :--- | :--- | :--- |
| 1-4 | completed | — | 调查 / 实现 / 跑测试 / 提交 |
| 5 | **in_progress** | **verify** | `[VERIFY] Run the plan's acceptance checks with a reviewer subagent…` |
| 6 | pending | — | `Add Response.iter_json()/aiter_json() with JSON streaming media type validation` |

系统 `[VERIFY]` 待办由 `todolist/service.py::_normalize_verify` 插入（条件：非 verify 工作项 ≥
`TODOLIST_INFRA["verify_min_work_todos"]`（默认 2）），插入本身是生效的。但它停在 `in_progress`，
第 6 项也还是 `pending`，运行就结束了。

根因：计划未完成时的续跑由 `PlanContinuationEnforcer` 触发，而它的 `_inject` 需要
`runtime/hooks.py::MAYBE_TRIGGER_AUTO_TURN` —— **该 hook 由 server 层注册**（`server/__main__.py`）。
评测 harness 没有 server，`_inject` 拿到 None 直接 `return False`（无日志），所以：
计划未完成 → 没有续跑 → `[VERIFY]` 永远不跑 → 回合在第一次流结束后就收尾。

修复（`evals/deepswe/suite.py`）：
- 每轮结束后读沙箱的 `stores/registry.db`（todos + task_flows），得到未完成项；
- 若还有未完成项，最多再驱动 6 轮 `[SYSTEM DIRECTIVE: PLAN CONTINUATION]`（列出未完成 todo/flow），
  直到计划清空或达上限；
- 报告新增 `continuations`。
- 单测：`tests/evals/test_deepswe_continuation.py`（2 个）。

注意一个产品侧边界：模型只建 **1 个工作项**时（本次 23:00 的复跑就是），
`verify_min_work_todos=2` 使 `[VERIFY]` 根本不会插入 —— 单步任务等于没有强制自检。
是否把阈值降到 1、或对“任务型”单步也插 verify，需要产品决策。

## 12. 阈值降到 1 后的验证（2026-11-19 23:19 → 00:06，httpx + reasoner）

改动：`verify_min_work_todos` 2 → **1**（提交 `86c46426`），四语言 wiki 同步；续跑读 registry 的路径
修正（`sandbox/stores/registry.db`，提交 `278b0cbe`）。

运行结果：

| 指标 | 数值 |
| :--- | :--- |
| 轮次 | **7 轮**（首轮 + 6 次续跑，达 cap） |
| 工具调用 | 192 |
| 耗时 / tokens | **2977s / 61.5M 输入** |
| F2P / P2P | 87/108 / 1404/1404 |
| 编排 | todolist=14，taskflow=12，compliant |

**机制验证成功**：
- `[VERIFY]` 待办在每轮计划里都插入了（#11/#13/#15/#17/#19 completed，#21 pending）；
- 续跑真实驱动了 6 个额外回合（`continuations=6`）。

**但暴露出新问题（Bug #24）**：模型把「自检」变成了空转——
每次续跑都新建一个 “Closure flow for the N-th verification pass”，把 verify todo 标 completed，
reviewer 也放行；但隐藏测试要的 media-type 校验始终没实现，F2P 仍停在 87/108。
首轮 flow 以 `failed` 结束，之后 5 个 verify flow 全 `done` —— 自检在“流程上通过、行为上没通过”。

原因分析：
1. reviewer 校验的是计划/instruction 的语义，没有逐条对照 instruction 的硬性条款（这里第 3 行
   明确要求非 JSON media type 抛 `DecodingError`），所以「验收通过」不等于「需求满足」；
2. 续跑提示词写的是「完成未完成项并关闭 flow」，模型于是不断开新的 closure flow 来“完成”，
   形成**验证空转**；
3. 代价很高：6 次续跑 = 50 分钟、61M 输入 token，分数反而低于无续跑的 99/108。

建议的修补（下一步，未实施）：
- **无进展停止**：续跑前对比 `git diff`，若上一轮没有新增改动则不再续跑（直接收尾并标记未完成）；
- **需求清单驱动**：把 instruction 里可判定的条款沉淀成 checklist（属于 [VERIFY] todo 的验收项），
  reviewer 必须逐条给出通过/不通过，而不是只“读计划”；
- 续跑 cap 从 6 降到 2–3，避免长尾空转。

## 13. 收尾：自检改为无条件（2026-11-20）

按产品决定，`[VERIFY]` 不再判断工作项数量：

- `verify_min_work_todos` 配置项**整体删除**（TypedDict 与默认值），`_normalize_verify` 只保留
  「计划为空才移除 verify」这一结构性守卫；任何非空计划的最后一步都是自动 `[VERIFY]`；
- 同步更新：`AGENTS.md`、`wiki/loop-prevention/` 四语言、`workspace/template/*/AGENTS.md`
  （四语言 persona 模板）、`todowrite` 工具描述、`test_verify_todo.py` 文档串；
- 测试：todolist + plan-continuation **262 passed**；doc links / parity / language 全 PASS。
