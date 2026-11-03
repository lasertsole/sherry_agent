# 计划：修复 ToolGuardrails status 桥接盲区 + 重复无效请求检测

- 建档日期：2026-11-03
- 来源：SWE-bench 20261103_010944（Sympy 0 diff / 90 次迭代耗尽）根因分析 + 兄弟项目（ZCode / CodeX / Claude Code / pi）横向对比
- 状态：**待评审**

## 问题

SWE-bench Sympy 实例中，主代理反复用 terminal 跑 `curl`/`git` 探测 GitHub，每次拿到
403 或空结果，连续 90 次模型调用后 `IterationBudget exhausted`，产出空 diff。

sherry **已有** `ToolGuardrails` 的 exact-failure 检测（同工具 + 同参数 + 报错 →
WARN at 2 / BLOCK at 5），但完全没拦住这个模式。根因是一个 **status 桥接 bug**：

```
agent 跑 curl → GitHub 403 → curl 退出码 22
  → terminal 工具 return "Exit code 22\n..." 纯字符串      ← 不是 raise ToolException
    → LangChain 包成 ToolMessage(status="success")         ← 不是 "error"
      → ToolGuardrails core.py:403: is_error = False       ← 永远不计数
        → exact_failure_counts 不递增 → WARN/BLOCK 不触发
```

`web_fetch` 同理——`safe_fetch.py:219-226` 对任何 HTTP 响应（含 403/404/500）都设
`ok=True`，返回纯字符串，ToolMessage status 始终 `"success"`。

只有 `raise ToolException` 且 `handle_tool_error=True` 才产生 `status="error"`。
terminal 仅在危险命令 / 敏感文件 / 沙箱失败时 raise，命令本身的非零退出码不走这条路。

**影响范围**：不只是 SWE-bench——任何 terminal 命令失败（编译错误、测试失败、网络
不通）和任何 web_fetch 4xx/5xx 都被 ToolGuardrails 视为成功，failure 计数器盲区。

## 兄弟项目横向对比

| 机制 | ZCode | CodeX | Claude Code | pi | sherry（现状） |
|------|-------|-------|-------------|-----|---------------|
| 重复相同工具调用检测 | ✅ model-anomaly：签名级（tool 名 + stable JSON 参数），3 次连续相同 → 注入 reminder，**不看结果成败** | ❌ 仅 approval denial 的 circuit breaker | ❌ 无 | ❌ 无 | ⚠️ 有，但只看 `status=="error"`，命令失败报 success → 盲区 |
| 迭代预算 | 软提醒（可选 toolCallWarningThreshold） | token 级 rollout，无调用次数上限 | max_turns=8（Py）/ max_iterations（Rust，默认无限） | 无上限 while(true) | 90 次 model call |
| circuit breaker | compact rapid-refill：3 次连续 refill → 终止 turn | Guardian denial 3 次 → abort turn；WS→HTTP session 级熔断 | recovery_recipes Exhausted/Escalated（场景级） | overflow-recovery 一次性 flag | recovery mode：2 次 BLOCK → HALT，但 exact-failure 在 BLOCK 前因 status 盲区不计数 |
| 4xx 处理 | — | HTTP 层 403 不可重试，fast-fail | 403 不在 retryable_status，fast-fail | — | web_fetch 对 403 报 ok=True，不 fast-fail 也不计 error |

**关键差异**：ZCode 的 model-anomaly guard 只看**调用签名**不看结果 status，所以 curl
返回 403 还是 200 都不影响检测。sherry 的 ToolGuardrails 只看 `status=="error"`，
命令成功执行但返回错误内容（403 body / 非零退出码）完全穿透。

## 修复方案

### 第一层：修 status 桥接（P0，最小改动让现有 ToolGuardrails 生效）

这是真正的 bug——工具执行失败但报 success。

**关键区分：网络探测 vs 本地命令失败**。直接把所有非零退出码改为 `raise ToolException`
会影响开发流程（编译错误、测试失败是正常的迭代环节，agent 需要读 failure 输出修 bug，
BLOCK terminal 会卡死迭代）。因此 P0 分两类处理：

#### P0a：网络类命令 → raise ToolException（状态桥接修复）

只对**网络探测类失败**（curl/wget/git remote 等无法连通的情况）raise，让 ToolGuardrails
的 exact-failure 计数器生效。这是 SWE-bench Sympy 实例的根因。

**terminal 工具**（`agent/tools/terminal/core.py`）：

在 `_execute_sync`（~line 480-481）和 `_run`（~line 829-835）的退出码判断中，
增加网络命令检测：

```python
_NETWORK_COMMAND_PREFIXES = ("curl", "wget", "git clone", "git fetch",
                              "git pull", "git push", "git ls-remote",
                              "git remote", "http", "ssh")

def _is_network_failure(command: str, exit_code: int) -> bool:
    """网络探测命令的非零退出码 → 视为工具错误（触发 ToolGuardrails 计数）。
    本地命令（编译/测试/运行）的非零退出码 → 保持 return（agent 需读输出迭代）。"""
    if exit_code == 0:
        return False
    cmd_lower = command.strip().lower()
    return any(cmd_lower.startswith(prefix) for prefix in _NETWORK_COMMAND_PREFIXES)

# 在退出码判断处：
if proc.returncode != 0:
    output = f"Exit code {proc.returncode}\n{stdout}\n{stderr}"
    if _is_network_failure(command, proc.returncode):
        raise ToolException(output)  # → ToolMessage(status="error")
    return output  # 本地命令失败 → status="success"，agent 读输出迭代
```

- `handle_tool_error=True` 已在 `build_terminal_tool`（line 868-871）设好，
  raise 会自动转成 `ToolMessage(status="error")`，内容 = 异常消息（完整 stdout/stderr）。
- **不改变输出内容本身**（exit code + stdout/stderr 仍传给模型），只改 status。
- terminal_bg 同理（`agent/tools/terminal/bg_tools.py` ~line 134）。

**web_fetch 工具**（`agent/tools/web/web_fetch.py`）：

在 `~line 218` 返回前检查 `result.status`，>= 400 时
`raise ToolException(f"HTTP {result.status}\n{content}")`。

- 或改 `pub/func/validator/safe_fetch.py:219-226` 让 4xx/5xx 设 `ok=False`
  （影响面更广，需评估其他消费者）。**推荐改 web_fetch 本身**，不动 safe_fetch。

**改完效果**：curl 连续 403 → 2 次 WARN / 5 次 BLOCK，现有 ToolGuardrails 链路
直接生效。本地命令失败（test/compile/run）不受影响，agent 可正常迭代。

#### P0b：本地命令失败 → metadata 标记（不 BLOCK，但可观测）

本地命令（编译/测试/运行）的非零退出码保持 `return`（status="success"），
但在 ToolMessage metadata 中标记 `exit_code`：

```python
# terminal _execute_sync 返回时：
return ToolOutput(
    content=output,
    metadata={"exit_code": proc.returncode},  # 模型和中间件可读
)
```

**注意**：LangChain 的 `ToolMessage` 支持 `additional_kwargs` 传 metadata。
如果 terminal 工具当前不返回 metadata，需要确认 `ToolMessage` 的构造路径
是否透传 metadata。如果无法透传，P0b 可跳过（不影响 P0a 的核心修复）。

P0b 是**可选的增强**——让 P1 的 streak 检测能区分"成功但退出码非 0"和"真正成功"，
但不阻塞 P0a 的落地。

### 第二层：加签名级重复检测（P1，ZCode parity，不看 status）

即使修了 status 桥接，agent 只要每次换一点参数（不同 URL、不同命令变体），
`args_hash` 就不同，exact-failure 计数器归零。ZCode 的做法更宽：**只看连续相同
签名，不看结果**。

在 `ToolGuardrails` 或新中间件里加 streak 追踪：

**签名计算**（必须稳定，相同参数 → 相同签名）：

```python
import hashlib, json

def _tool_call_signature(tool_name: str, args: dict) -> str:
    """计算工具调用签名。相同 tool + 相同参数（含 key 顺序无关）→ 相同签名。"""
    # sort_keys=True 保证 dict key 顺序不影响 hash
    # ensure_ascii=False 保证中文参数不产生不同 bytes
    # 去掉 None 值的 key（避免 None vs 缺失 key 的差异）
    cleaned = {k: v for k, v in args.items() if v is not None}
    args_str = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
    args_hash = hashlib.md5(args_str.encode("utf-8")).hexdigest()[:12]
    return f"{tool_name}:{args_hash}"
```

**streak 追踪逻辑**：

- 每次 `wrap_tool_call` 后计算签名 `sig = _tool_call_signature(tool_name, args)`。
- 与 `last_sig` 比较：相同 → `streak += 1`；不同 → `streak = 0, last_sig = sig`。
- `streak >= 3`（ZCode 默认阈值）→ 注入 `<system-reminder source="tool_repetition">`
  "你已经连续 3 次用相同参数调用 {tool_name}，结果未变。改变策略或停止重复。"
- 注入后 `streak = 0`，`max_warnings_per_turn = 3` 上限防止刷屏。
- 跨 turn 不持久化（与现有 ToolGuardrails 一致，turn 边界重置）。
- **排除终端命令**：terminal 工具的参数包含命令字符串，`curl http://a` 和
  `curl http://b` 是不同签名（正确），但 `pytest tests/test_a.py` 连续跑 3 次
  可能是合理的（等测试结果）。terminal 的 streak 阈值提高到 5（配置项
  `streak_terminal_warn_after = 5`），其他工具保持 3。

**覆盖范围补集**：exact-failure 覆盖"失败且报 error"；streak 覆盖"成功但无用"
（如 `git log` 反复跑、`read_file` 反复读同一文件、`web_search` 反复搜同关键词）。

**参考实现**：ZCode `apps/zcode-cli/packages/core/src/runtime/helpers/model-anomaly.ts`
的 `detectRepeatedToolCallWarnings`（lines 23-62）和
`buildRepeatedToolCallSignature`（lines 111-127）。

### 第三层：网络感知策略注入（P2，eval 专用）

RESULTS.md 已建议但没实现。

- `_probe_web`（`evals/swebench/suite.py:96-134`）探测到 `terminal_egress=false` 后，
  在 `_SWEBENCH_AGENTS_APPENDIX`（suite.py:73-84）动态拼接一条 directive：
  > "网络不可达（GitHub API 返回 403）。不要继续探测网络，改为基于本地代码和测试修复问题。"
- 这不替代第一/二层，而是让 agent 一开始就知道网络不通，减少无谓探测的第一次尝试。

### 第四层：解开 SWE-bench 委派封印（P2，让 subagent 分流探测工作）

即使防住了重复探测，单 agent 在 90 次预算内修复杂 bug 仍吃力。

| 改动 | 文件 | 效果 |
|------|------|------|
| 默认 `--persona coding` | `evals/coding_capability/suite.py:154` | 系统提示带 `sessions_spawn` 指引 |
| 删 `_PROCEED_DIRECTIVE` 里的 "do not delegate" | `suite.py:108-111` | HITL resume 不再反向压制委派 |
| 设 `SHERRY_EVAL_DELEGATION_PRESSURE=1` | 运行命令 | 激活 `suite.py:715` 子代理等待逻辑 |

## 涉及文件清单

| 层 | 文件 | 改动 |
|----|------|------|
| P0a | `agent/tools/terminal/core.py` (~480, ~829) | 网络命令非零退出码 raise ToolException；新增 `_is_network_failure` |
| P0a | `agent/tools/terminal/bg_tools.py` (~134) | terminal_bg 同步网络命令 raise |
| P0a | `agent/tools/web/web_fetch.py` (~218) | HTTP >= 400 raise ToolException |
| P0b | `agent/tools/terminal/core.py` | 本地命令非零退出码 → metadata 标记 exit_code（可选） |
| P1 | `agent/middlewares/tool_guardrails/core.py` 或新中间件 | 加 streak 签名检测 + `_tool_call_signature` |
| P1 | `config/features/agent_side/tool_guardrails.py` | 加 `streak_warn_after=3` / `streak_terminal_warn_after=5` 配置 |
| P2 | `evals/swebench/suite.py` (73-84, 96-134) | 动态网络感知 directive |
| P2 | `evals/coding_capability/suite.py` (108-111, 154) | 解开委派封印 |

## 验收

1. **P0a 单元测试**：terminal 跑 `curl http://403-url`（退出码 22）→ ToolMessage status="error"；
   `curl` 连续 403 → 5 次 BLOCK。terminal 跑 `pytest`（退出码 1）→ ToolMessage status="success"
   （本地命令不 raise），内容包含 stderr。
   web_fetch 403 → status="error"。
2. **P0a 集成测试**：模拟连续 5 次相同 `curl 403` → 第 5 次被 BLOCK，
   ToolMessage 返回 block 信息，工具加入 blocked_tools。
3. **P0a 不影响本地命令**：连续 3 次 `pytest` 失败 → 不被 BLOCK（status="success"），
   agent 可读输出迭代。
4. **P1 单元测试**：
   - 连续 3 次相同参数调用（即使 status=success）→ 注入 reminder
   - 第 4 次参数变化 → streak 重置
   - `json.dumps(sort_keys=True)` 验证：`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算出相同签名
   - terminal 工具 streak 阈值 5（其他工具 3）
5. **SWE-bench 回归**：重跑 Sympy 实例，agent 在网络不通时不再反复探测 GitHub，
   在预算内产出非空 diff。
6. **不影响正常流程**：编译错误、测试失败的 terminal 输出内容不变（只改 status），
   evidence collector 正常记录（网络命令 status=error 但 evidence 命令是本地命令 status=success）。

**跨计划交互**：
- `计划_重启恢复收口_EffectQueue.md` 的 auto-turn / continuation handler 不依赖 terminal，
  P0a 的 status 变更不影响 EffectQueue。
- evidence verification 命令（`taskflow_flow(action="finish")` 的 regression gate replay）
  通过 terminal 跑——这些是本地命令（pytest/ruff/mypy），P0a 不 raise，status="success" 不变。
  只有网络探测类命令受影响。
- `计划_SWE_0of2修复_20261103.md` 的 Sympy 根因（90 次 terminal 探测 GitHub 403）
  由本计划 P0a 解决。该计划的 Sympy 部分应引用本计划而非独立提"不要反复探测 GitHub"。
