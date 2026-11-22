# DeepSWE 本地模式运行记录

> **历史存档（2026-11-22）**：`--local` 模式（`evals/deepswe/local.py`）已随容器
> 路径在 podman 下的打通而删除；本文件保留其历史测量，不再更新。现行容器模式的
> 运行结果记录在各自的 `evals/results/deepswe/<run_id>/`。

本文件记录 `--local` 模式的真实运行结果。报告和补丁在 gitignored 的
`evals/results/deepswe-local/<run_id>/`；任务 checkout 在
`~/.cache/sherry_evals/deepswe-local-work/<run_id>/`。本地模式不替代官方
Docker DeepSWE 分数，详见 `README.md`。

---

## 20261101_233217 — 默认切片（2 条任务）

- 命令：`uv run --no-sync python evals/deepswe/suite.py --local --keep-workdirs --timeout 1200`
- 模型：`deepseek/deepseek-flash`，thinking off
- GOLD 自检（先于模型调用）：两任务均 `checked` 并通过
- 汇总：**0/2 resolved**，无 invalid run；总 2595.5s
- 两任务均在 1200 秒硬期限被杀；部分补丁仍由官方 `grader.py` 评分

| 任务 | Resolved | F2P | P2P | 结果 | 耗时 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `httpx-streaming-json-iteration` | ✗ | 104/108 | 1404/1404 | `timeout` | 1293.8s |
| `cattrs-partial-structuring-recovery` | ✗ | 62/69 | 7/7 | `timeout` | 1219.1s |

### `httpx-streaming-json-iteration`

改动路径：`httpx/_json_stream.py`、`httpx/_models.py`，另有一个未被测试使用的
`test` 文件。门禁未判为测试篡改。

4 个 F2P 失败，全部在 JSON 编码检测/流式边界：

1. `test_iter_json_document_respects_json_text_encoding_detection[utf-32-le]`
   — `DecodingError`，UTF-32-LE 文本被直接交给 `jsonlib.JSONDecoder.raw_decode`。
2. `test_iter_json_document_respects_json_text_encoding_detection[utf-32]`
   — 同上；UTF-32 字节序没有被识别。
3. `test_iter_json_document_streaming_chunk_boundaries[chunks2-expected2]`
   — `' \ufeff["x"]'` 的 BOM 出现在前导空格之后，`_strip_bom` 只处理文本开头，
   结果在 BOM 处解析失败。
4. `test_iter_json_json_seq_non_utf8_encodings[utf-32-le]`
   — JSON-sequence 的 UTF-32-LE 路径同样未做编码检测。

根因是补丁只覆盖了部分编码/BOM 分支，没有把响应字节的 charset 检测接入所有
JSON 读取入口；不是 harness 错误也不是 P2P 回归。

### `cattrs-partial-structuring-recovery`

改动路径：`src/cattrs/__init__.py`、`src/cattrs/converters.py`，以及 agent 自己
生成的 `VERIFY_REPORT.txt`、`scratch_check.py`；未改 `tests/`。

7 个 F2P 失败：

1. `TestPartialResultExport.test_top_level_function`
   — `module 'cattrs' has no attribute 'partial_structure'`（顶层函数未导出）。
2. `TestPartialResultExport.test_top_level_matches_global_converter`
   — 同一个缺失导出。
3. `TestRefine.test_chain`
   — 修正多个失败字段后 `PartialResult.refine` 仍返回 `value=None`。
4. `TestRefine.test_nested_object`
   — 嵌套对象 refine 后 `value` 为 `None`。
5. `TestRefine.test_required_field_fixed`
   — 必填字段修复后 `refined.value` 仍为 `None`。
6. `TestRefine.test_typeddict`
   — TypedDict 修复后缺少仍为失败的 `'a'` 键。
7. `TestTypedDictPartialStructuring.test_required_field_fails`
   — 必填字段失败时返回了部分 dict，而判据要求 `value is None`。

根因是 `PartialResult.refine` 没有把新值合并回原始输入并重新结构化；必填字段失败
的语义也与测试期望不一致。P2P 7/7 未回归。

### 记录口径

- 两个任务都是 **模型未在 1200 秒内完成**，不是 GOLD/harness 失败；`invalid_runs=[]`。
- 硬超时由 `evals/deepswe/agent_worker.py` 的独立进程组执行；被杀的 worker 没有
  写出最终 `agent-result.json`，因此报告里的 `tool_calls=0`、`tokens={}` 是
  “超时未采集”，不是主 agent 真的没有调用工具。
- 失败补丁和逐测试失败信息在：
  `evals/results/deepswe-local/20261101_233217/`。

---

## 20261102_083002 — 方案 C（无 WebSocket busy steering）验证

- 命令：`uv run --no-sync python evals/deepswe/suite.py --local --skip-self-check --keep-workdirs cattrs-partial-structuring-recovery --timeout 1200`
- 目的：验证无 WebSocket 时，子代理完成消息会进入 `SteeringQueue`，并由
  `SubagentCompletionDrainMiddleware` 在下一模型调用前注入。
- 结果：**未超时**，537.5s；0/69 F2P，7/7 P2P；P2P 无回归，agent 未解决任务。
- 日志证据：08:34、08:36 等出现
  `completion drain: injecting 1 subagent completion message(s)`；主 transcript 中
  能看到 `[subagent:... completed]` 的 carrier。
- 仍然失败的原因：
  1. 第一个实现子代理以 DeepSeek `reasoning_content` 400 结束；
  2. 压力门在 16 次读后阻断读写，主 agent 看到完成结果却无法修改
     `src/cattrs/partial.py`，只能反复派只读状态检查子代理；
  3. 补丁只有 `src/cattrs/partial.py`，缺少 `__init__.py` 导出和
     `converters.py` 的 `refine` 修复。
- 结论：方案 C 解决了“子代理结果不可见”，但压力门的恢复窗口和子代理 thinking
  400 仍是独立阻塞项。

---

## 20261116_005543 — httpx 单任务复跑（1/1 resolved）

- 命令：`uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200 httpx-streaming-json-iteration`
- 结果：**resolved=True**；108/108 F2P、1404/1404 P2P；70 次工具调用 / 808.6s；
  tokens 2,919,410 / 18,972；`invalid_runs=[]`。
- 根因：上一轮在 37 次调用 / 346.3s 后连续 5 次流停滞，`LLMRetryMiddleware` 触发
  stale-streak 熔断。用该会话最后的 73 条消息 + 一个新建客户端复现，**2.5s 就完成**，
  说明不是对话内容或 payload；而是 agent 中间执行了 `sleep 60` 的 terminal 命令，
  长时间空闲后复用了 provider/代理已经关闭的 keep-alive 连接。5 次重试都在同一个
  坏连接池上等满 60s 读超时，才看起来像“provider 连续挂 5 次”。
- 修复：超时后 `NormalizingChatModel.reset_async_client()` 关闭并重建 inner 的
  HTTP 客户端，下一次重试使用新连接；stale-streak 熔断仍保留，用于真正无响应的
  provider。单测覆盖客户端重建和 retry 路径。
- 备注：本轮 agent 用了 70 次调用、808.6s，未再触发熔断，最终 108/108。

---

## 20261115_233700 — 默认切片（2 条任务，P0 修复后）

- 命令：`uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200`
- 模型：`deepseek/deepseek-flash`，thinking off
- GOLD 自检：两任务均 `checked` 并通过。
- 汇总（原始报告口径）：**0/2 resolved**，843.2s；`httpx` 被标成 `harness`
  error（分类 bug，见下），`cattrs` 是有效运行。

| 任务 | Resolved | F2P | P2P | 结果 | 耗时 | 调用 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `httpx-streaming-json-iteration` | ✗ | 107/108 | 1404/1404 | provider stall（报告旧口径：`harness`） | 346.3s | 37 |
| `cattrs-partial-structuring-recovery` | ✗ | 62/69 | 7/7 | 有效，未解决 | 411.8s | 51 |

### 两个缺陷已修

1. **`KeyError: 'tools'`（先修后重跑）**：`bind_external_session` 仍读
   `payload["tools"]`，但 preset 在 2026-11-05 起只携带 `skills`（工具是构建期
   契约）。旧代码让 worker 在第一次模型调用前就崩溃，两个任务都变成
   `harness error` + `tool_calls=0`。已移除该读取并加回归测试。
2. **provider stall 被误分类**：`httpx` 在 37 次调用 / 346.3s 时连续 5 次流停滞，
   `LLMRetryMiddleware` 以 `Provider unresponsive — aborting to avoid indefinite
   stall.` 中止，报告却把 `failure_kind` 记为 `harness`，会污染能力统计。现在
   `provider unresponsive` 进入 provider 标记，`invalid_run=True`，不计入
   resolve rate；有单测钉住。

### 结果解读

- `httpx` 在 provider 中止前已经拿到 **107/108 F2P、1404/1404 P2P**，只差
  UTF-32/JSON-sequence 相关的一个用例；这是 provider 中止造成的 near-miss，
  不应算作 harness 或能力结论。
- `cattrs` 是完整有效运行，62/69 F2P、7/7 P2P；剩余 7 个失败仍是
  `PartialResult.refine` 的合并/必填字段语义，不是回归。
- P0-3 部分遥测在真实运行中被观察到：`agent-result.json` 在任务进行中已有
  `partial: true`、`tool_calls=37`、`tokens={input:1047513, output:8320}`；本轮
  两个任务都在 1200s 前自然结束，没有触发硬超时路径。

---

