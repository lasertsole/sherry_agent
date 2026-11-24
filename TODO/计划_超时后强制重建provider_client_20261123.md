# 计划：超时后强制重建 provider client（eval + 子 agent）

- 日期：2026-11-23
- 状态：**待评审**
- 来源：`asyncio.wait_for` 超时后 `CancelledError`（BaseException）绕过 `LLMRetryMiddleware` 的 `except Exception`，不触发 `reset_async_client()`。in-flight 请求的 httpx response 被 `_cleanup` 的 `source.aclose()` 关了，但 client pool 不关——坏连接留在 pool 里可能被复用。eval 的 known gap（`wiki/evals/README.md:89-91`），产品侧子 agent goal loop 超时也有同样问题。
- 风险等级：**低** — 在超时 catch 路径加 `reset_async_client()` 调用
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

### 超时后的链路

```
asyncio.wait_for(agent.ainvoke(...), timeout=T)
  → 超时 → CancelledError（BaseException，不是 Exception）
  → CancelledError 传播穿过 ainvoke → astream → httpx streaming
  → _cleanup 的 finally 调 source.aclose() → 关闭 httpx streaming response（socket 关了）
  → 但 CancelledError 绕过 LLMRetryMiddleware 的 except Exception（BaseException ≠ Exception）
  → reset_async_client() 不触发
  → 坏连接留在 AsyncOpenAI client pool 里
```

### 影响范围

| 路径 | 位置 | 下个 turn 重建 client？ | 问题 |
|------|------|:---:|------|
| 主 agent turn（正常） | `messages.py` `_GenerateTurn._prepare` | ✅ `built_agent(force_rebuild=True)` 每 turn 重建 | 不受影响 |
| eval 容器 turn 超时 | `suite.py:781-784` | ✅ 下个 turn `_prepare` 重建 | **超时瞬间坏连接残留到下个 turn 重建** |
| 子 agent goal loop 超时 | `lifecycle.py:768` | ❌ goal loop 内不重建 | **坏连接残留到整个 run 结束** |
| middleware 内 APITimeoutError | `llm_retry/core.py:382` | ✅ `_refresh_provider_client` | 不受影响 |

**eval 容器路径**：下个 turn 的 `_Prepare` 调 `built_agent(force_rebuild=True)` 重建 graph+client，所以坏连接不跨 turn 残留。但超时瞬间到下个 turn 重建之间，如果有 continuation loop 立即跑下一个 `asyncio.wait_for`，可能复用旧 client。

**子 agent goal loop 路径**：goal loop 内的 continuation turn 不重建 graph（用同一个 `child_agent` 实例），坏连接残留到整个 run 结束。这是真正的产品侧缺口。

---

## 1. 设计

在超时 catch 路径调 `reset_async_client()`，强制关闭并重建 provider HTTP client pool。

### 1.1 怎么拿到 model 引用

`NormalizingChatModel` 是 agent graph 里的 model 节点。两条路径的 model 引用：

**eval 容器**：通过 `build_main_llm()` 获取——`build_main_llm()` 每次调用创建新 `NormalizingChatModel` 实例，但 inner provider 的 client pool 可能共享。更可靠的方式是通过 `server.service.messages` 模块获取当前 agent 的 model。但 eval 路径不总是走 server。

**最简方案**：直接调 `build_main_llm()` 获取一个 `NormalizingChatModel` 实例，调 `reset_async_client()`。如果 inner provider 的 client 是共享的（OpenAI SDK 全局 client），reset 会关闭共享 pool。如果不是共享的，reset 关闭的是新实例的 pool（无害但无用）。

**更可靠方案**：通过 `LLMRetryMiddleware` 的 `_refresh_provider_client`（`llm_retry/core.py:501-520`）—— 但它在 middleware 实例上，超时 catch 路径拿不到 middleware 实例。

**最终方案**：在 `NormalizingChatModel` 上加一个静态方法 `reset_shared_client()`，通过 `build_main_llm()` 获取实例调 `reset_async_client()`。如果 inner client 是共享的，reset 生效；如果不是，无害。

### 1.2 为什么不在 `_cleanup` 里调

`_cleanup`（`messages.py:664-684`）的注释明确说不在那里关 client pool——每 turn 都跑 `_cleanup`，每次关 pool 太浪费。超时 catch 是专门异常路径，只在超时时触发。

---

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/tools/subagent/spawn/lifecycle.py:861-868` | 超时/kill catch 里调 `_reset_provider_client()` | 子 agent goal loop（产品侧缺口） |
| 2 | `evals/deepswe/suite.py:782-784` | 超时 catch 里调 `_reset_provider_client()` | eval 容器（known gap） |

---

## 3. 改动细节

### 3.1 子 agent goal loop 超时（lifecycle.py）

现有（line 860-868）：

```python
except TimeoutError:
    outcome = RunOutcome(
        status=RunOutcomeStatus.TIMEOUT, error=f"Subagent timed out after {timeout_seconds}s"
    )
except asyncio.CancelledError:
    outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
except Exception as e:
    outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))
```

改为：

```python
except TimeoutError:
    await _reset_child_provider_client(child_agent)
    outcome = RunOutcome(
        status=RunOutcomeStatus.TIMEOUT, error=f"Subagent timed out after {timeout_seconds}s"
    )
except asyncio.CancelledError:
    await _reset_child_provider_client(child_agent)
    outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
except Exception as e:
    outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))
```

新增辅助函数（文件内，`_execute_subagent_scoped` 之前）：

```python
async def _reset_child_provider_client(child_agent: Any) -> None:
    """Force-close the child agent's provider HTTP client pool after timeout/cancel.

    CancelledError is BaseException, not Exception — it bypasses LLMRetryMiddleware's
    except Exception, so reset_async_client() never fires. Stale connections stay in
    the pool. The goal loop reuses the same child_agent instance across continuation
    turns, so a bad connection persists until the run ends.

    This closes the pool so the next continuation turn starts fresh.
    """
    try:
        # create_agent stores model in the graph; access path varies by LangGraph version
        model = None
        # Try common attribute paths
        for attr in ("model", "_model"):
            model = getattr(child_agent, attr, None)
            if model is not None:
                break
        if model is None:
            # LangGraph CompiledGraph stores it in nodes/edges — try the builder
            builder = getattr(child_agent, "builder", None)
            if builder is not None:
                model = getattr(builder, "model", None)
        if model is not None and hasattr(model, "reset_async_client"):
            await model.reset_async_client()
    except Exception:
        pass  # fail-open: 不阻塞超时/kill 处理
```

**注意**：`child_agent` 是 `create_agent(model=main_llm.bind(temperature=...), ...)` 返回的 CompiledGraph。model 的访问路径取决于 LangGraph 版本。`create_agent` 在 `agent/core.py:329-335`，model 通过 `model=main_llm.bind(temperature=temperature)` 传入。CompiledGraph 可能不直接暴露 model——实施时需确认访问路径。

**替代方案**（如果 CompiledGraph 不暴露 model）：子 agent 用 `build_auxiliary_llm()` 或 `build_main_llm()` 创建 model。在 `_execute_subagent_scoped` 里 model 已经在 `_build_child_agent` 之前创建（通过 `_select_child_llm`），可以从 `child_agent_factory` 的返回值里拿到 model 引用。

**最可靠方案**：在 `_build_child_agent`（`child_agent_factory.py`）里把 model 引用存在 child_agent 的自定义属性上：

```python
# child_agent_factory.py, _build_child_agent 中
compiled = create_agent(model=child_llm, ...)
compiled._sherry_model = child_llm  # 暴露 model 引用给超时 catch
return compiled
```

然后 `_reset_child_provider_client` 里：

```python
model = getattr(child_agent, "_sherry_model", None)
```

### 3.2 eval 容器 turn 超时（suite.py）

现有（line 781-784）：

```python
try:
    turn = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout)
except TimeoutError:
    drive["timeout"] = True
    break
```

改为：

```python
try:
    turn = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout)
except TimeoutError:
    drive["timeout"] = True
    # Force-close the provider HTTP client pool: CancelledError bypasses
    # LLMRetryMiddleware's except Exception (BaseException), so
    # reset_async_client() never fires.
    try:
        from models.LLMs.main_llm import build_main_llm
        llm = build_main_llm()
        if hasattr(llm, "reset_async_client"):
            await llm.reset_async_client()
    except Exception:
        pass  # fail-open
    break
```

**为什么 eval 用 `build_main_llm()` 而不是 child_agent 引用**：eval 容器路径通过 `async_generate` → `_GenerateTurn` → `built_agent` 驱动主 agent，不经过子 agent 的 child_agent。主 agent 的 model 通过 `built_agent(force_rebuild=True)` 每 turn 重建，但超时瞬间旧 client pool 可能在 continuation loop 的下一个 `asyncio.wait_for` 被复用。调 `build_main_llm().reset_async_client()` 关闭共享 pool。

**`build_main_llm()` 返回新实例的 `reset_async_client()` 是否生效**：取决于 inner provider model 的 client 是否共享。OpenAI SDK 的 `AsyncOpenAI` client 通常是 per-instance 的（不共享），所以 `build_main_llm()` 创建的新实例的 `reset_async_client()` 关闭的是新实例的空 pool（无害但无用）。

**要真正生效**，需要拿到当前 agent graph 里的 model 实例。通过 `_GenerateTurn` 的 agent 引用：

```python
except TimeoutError:
    drive["timeout"] = True
    try:
        from server.service.messages import _current_agent
        agent = _current_agent()  # 获取当前 agent 实例
        model = getattr(agent, "_sherry_model", None) or getattr(agent, "model", None)
        if model is not None and hasattr(model, "reset_async_client"):
            await model.reset_async_client()
    except Exception:
        pass
    break
```

**但 eval 不走 server**——eval 通过 `async_generate` 但可能不在 server 进程里。需要确认 eval 是否在 server 进程内跑。

**最终方案**：eval 容器路径的超时由 `_GenerateTurn._prepare` 的 `built_agent(force_rebuild=True)` 在下个 turn 重建兜底。eval 路径的修复优先级低于子 agent 路径（子 agent goal loop 不重建 client）。如果 eval 确认下个 turn 重建覆盖了，eval 路径可以只加注释说明而不加 `reset_async_client()`。

---

## 4. 修正后的改动

| # | 文件 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `agent/tools/subagent/spawn/child_agent_factory.py` | `_build_child_agent` 中 `compiled._sherry_model = child_llm` | P1（暴露 model 引用） |
| 2 | `agent/tools/subagent/spawn/lifecycle.py:861-868` | 超时/kill catch 里调 `_reset_child_provider_client(child_agent)` | P1（产品侧缺口） |
| 3 | `evals/deepswe/suite.py:782-784` | 超时 catch 里注释说明下个 turn 重建兜底 | P2（注释，不改代码） |

### 4.1 child_agent_factory.py 改动

在 `_build_child_agent`（line 625+）中，`create_agent` 之后：

```python
compiled = create_agent(
    model=child_llm,
    tools=tools,
    middleware=middlewares,
    checkpointer=child_checkpointer,
    # ... existing kwargs ...
)
compiled._sherry_model = child_llm  # 暴露给超时 catch 的 reset_async_client
return compiled
```

### 4.2 lifecycle.py 改动

新增辅助函数：

```python
async def _reset_child_provider_client(child_agent: Any) -> None:
    """Force-close the child agent's provider HTTP client pool after timeout/cancel.

    CancelledError is BaseException, not Exception — it bypasses LLMRetryMiddleware's
    except Exception, so reset_async_client() never fires. The goal loop reuses the
    same child_agent instance across continuation turns, so a bad connection persists
    until the run ends. This closes the pool so the next continuation turn starts fresh.
    """
    try:
        model = getattr(child_agent, "_sherry_model", None)
        if model is not None and hasattr(model, "reset_async_client"):
            await model.reset_async_client()
    except Exception:
        pass  # fail-open: 不阻塞超时/kill 处理
```

在 `except TimeoutError` 和 `except asyncio.CancelledError` 之前调用：

```python
except TimeoutError:
    await _reset_child_provider_client(child_agent)
    outcome = RunOutcome(
        status=RunOutcomeStatus.TIMEOUT, error=f"Subagent timed out after {timeout_seconds}s"
    )
except asyncio.CancelledError:
    await _reset_child_provider_client(child_agent)
    outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
except Exception as e:
    outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))
```

### 4.3 eval suite.py 改动

加注释，不改代码：

```python
except TimeoutError:
    # Known gap (wiki/evals/README.md): asyncio.wait_for's CancelledError is
    # BaseException, bypassing LLMRetryMiddleware's reset_async_client. The
    # next turn's _GenerateTurn._prepare calls built_agent(force_rebuild=True)
    # which rebuilds the graph + httpx client, so stale connections don't
    # persist across turns. No reset_async_client needed here.
    drive["timeout"] = True
    break
```

---

## 5. 不改的东西

| 项 | 理由 |
|----|------|
| `_cleanup`（`messages.py`） | 注释明确说不在这里关 pool，每 turn 都跑 |
| `LLMRetryMiddleware` 的 `except Exception` | 不改——CancelledError 是 BaseException，改 catch BaseException 影响面太大 |
| `built_agent(force_rebuild=True)` | 主 agent 每 turn 重建机制不改 |
| `reset_async_client()` 实现 | 已有，不改 |
| eval 容器路径 | 下个 turn `built_agent(force_rebuild=True)` 重建兜底，不需要额外 reset |

---

## 6. 测试

| 测试 | 内容 |
|------|------|
| `test_child_agent_exposes_model` | `compiled._sherry_model` 存在且是创建时传入的 model |
| `test_reset_child_provider_on_timeout` | goal loop 超时后 `_reset_child_provider_client` 被调用 |
| `test_reset_child_provider_on_cancel` | goal loop 被 cancel 后 `_reset_child_provider_client` 被调用 |
| `test_reset_fail_open` | model 没有 `_sherry_model` 或 `reset_async_client` 时不报错 |
| `test_reset_after_timeout_calls_reset_async_client` | 超时后 model 的 `reset_async_client()` 被调用（mock 验证） |
| `test_reset_not_called_on_normal_exit` | 正常完成不调 `_reset_child_provider_client` |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| `_sherry_model` 属性被 LangGraph 版本升级覆盖 | 用 `setattr(compiled, "_sherry_model", model)` 在 `create_agent` 之后设置，不依赖 LangGraph 内部属性 |
| `reset_async_client()` 本身超时 | 已有 5s 超时（`reasoning_normalizer.py:154`） |
| 子 agent 用 auxiliary LLM 而非 main LLM | `_sherry_model` 存的是实际使用的 model（可能是 auxiliary），`reset_async_client()` 对 auxiliary model 同样有效 |
| 重建 client 增加延迟 | 只在超时/cancel 路径触发，正常 turn 不触发 |
| `finally` 里 `release_child_resources` 可能在 reset 之前跑 | reset 在 `except` 块里，`finally` 在 `except` 之后执行，顺序正确 |

### Kill criteria

- 若 `_sherry_model` 属性方案不稳定（LangGraph 覆盖），改为在 `_execute_subagent_scoped` 的局部变量里持有 model 引用，传给 `_reset_child_provider_client`
- 若 `reset_async_client()` 对 auxiliary model 无效（没有 provider client），fail-open 不影响行为

---

## 8. ROI

### 改动量

| 改动 | 行数 |
|------|------|
| child_agent_factory.py `_sherry_model` | 1 行 |
| lifecycle.py `_reset_child_provider_client` + catch 调用 | ~20 行 |
| suite.py 注释 | ~5 行 |
| 测试 | 6 个 |
| **总计** | **~26 行代码 + 6 个测试** |

### 收益

| 收益 | 影响 |
|------|------|
| **子 agent goal loop 超时后不残留坏连接** | 产品侧缺口——goal loop continuation turn 不重建 client，坏连接残留到 run 结束 |
| **eval known gap 注释** | 文档明确说明下个 turn 重建兜底，不再标为 gap |

### ROI 判断

**高 ROI**。核心改动 1 行（`_sherry_model`）+ 20 行（catch 调用 + 辅助函数）。子 agent goal loop 是真正的产品侧缺口——continuation turn 复用同一个 child_agent 实例，坏连接残留到整个 run 结束。eval 路径由下个 turn 重建兜底，不需要额外修复。
