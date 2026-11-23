# 计划：Eval Temperature 统一为 0.0

- 日期：2026-11-23
- 状态：**待评审**
- 来源：coding_capability 钉 temperature=0.0，deepswe/devbench/multi_swe_bench/osworld 等跑产品默认 0.8。同类 coding benchmark 温度不同，跨 suite 比较无效。
- 风险等级：**低** — 加法式，不改产品默认
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

| Suite | Temperature | 位置 |
|-------|-------------|------|
| coding_capability | 0.0（钉住） | `evals/coding_capability/suite.py:119` |
| deepswe | 0.8（产品默认） | 不钉 |
| devbench | 0.8（产品默认） | 不钉 |
| multi_swe_bench | 0.8（产品默认） | 不钉 |
| osworld | 0.8（产品默认） | 不钉 |
| gaia | 0.8（产品默认） | 不钉 |

产品默认 `built_agent(temperature=0.8)`（`agent/core.py:357`）。eval 应该用 0.0 保证确定性输出和可复现性。

## 1. 改动

把 coding_capability 已有的 `pin_eval_temperature()` 提取到 shared，所有 coding benchmark suite 调用。

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `evals/shared/eval_temperature.py` | **新建**：提取 `pin_eval_temperature(temperature=0.0)` 函数 | 从 coding_capability 移到 shared |
| 2 | `evals/coding_capability/suite.py` | import 改为从 shared 导入 | 去重 |
| 3 | `evals/deepswe/suite.py` | `_run` 中调用 `pin_eval_temperature()` | 钉 0.0 |
| 4 | `evals/devbench/suite.py` | 同上 | |
| 5 | `evals/multi_swe_bench/suite.py` | 同上 | |
| 6 | `evals/osworld/suite.py` | 同上 | |
| 7 | `evals/gaia/suite.py` | 同上 | |

**不改**：产品默认 0.8（`agent/core.py:357`）、agentbench/toolbench/tau_bench/cybersec/webarena（非 coding benchmark，温度不强制）。

## 2. pin_eval_temperature 逻辑

从 `evals/coding_capability/suite.py:143-161` 提取，逻辑不变：

```python
def pin_eval_temperature(temperature: float = 0.0) -> None:
    """Monkeypatch built_agent to force a fixed temperature for eval runs.

    Eval needs deterministic output (temperature=0.0) for reproducibility.
    The product default is 0.8; this override pins it per-suite.
    """
    import server.service.messages as messages
    _original = messages.built_agent

    def _pinned(*args, **kwargs):
        kwargs["temperature"] = temperature
        return _original(*args, **kwargs)

    messages.built_agent = _pinned
```

每个 suite 在 `_run` 开头调 `pin_eval_temperature()`，在 `finally` 里不 restore（进程跑完即退出，和 coding_capability 现有行为一致）。

## 3. 验收

- 所有 coding benchmark suite 运行时 temperature = 0.0
- coding_capability 行为不变（只是 import 来源变了）
- 非 coding suite 不受影响
