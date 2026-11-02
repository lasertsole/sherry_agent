# DeepSWE 本地模式运行记录

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
