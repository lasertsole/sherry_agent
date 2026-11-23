# 计划：Eval Child Timeout 全局泄露修复

- 日期：2026-11-23
- 状态：**待评审**
- 来源：deepswe 的 `_eval_child_run_timeout_s()` 直接修改全局 `get_config().run_timeout_seconds`，不 restore。如果同一进程跑多个 suite，修改泄露到后续 suite。已在 `计划_子agent叶子超时600s提1800s_20261123.md` 里把乘数改为 1.0（和产品一致），但泄露问题本身需要独立修。
- 风险等级：**低** — 加 restore
- 依赖：`计划_子agent叶子超时600s提1800s_20261123.md`（乘数改 1.0 后泄露影响减小，但结构问题仍在）
- 可独立执行：✅

---

## 0. 问题

`evals/deepswe/suite.py:527-532`:

```python
_cfg = get_config()
if _cfg.run_timeout_seconds < _child_timeout:
    _cfg.run_timeout_seconds = _child_timeout
```

直接改全局 singleton，`EvalSandbox.restore()`（`suite.py:580`）不恢复。如果进程连续跑 deepswe + devbench，devbench 继承 deepswe 改过的值。

乘数改为 1.0 后（产品默认 1800 = eval 不补偿），bump 逻辑 `1800 < 1800` → False → 不触发修改。但 `SHERRY_EVAL_CHILD_RUN_TIMEOUT_S` 手动覆盖时仍会修改，且结构上是个坏模式。

## 1. 改动

| # | 文件 | 行 | 改动 | 说明 |
|---|------|----|------|------|
| 1 | `evals/deepswe/suite.py` | 527-532 | 记录原值，在 restore 里恢复 | 防泄露 |

### 改后

```python
# suite.py, _run 方法中
_original_run_timeout = get_config().run_timeout_seconds
try:
    _cfg = get_config()
    _child_timeout = _eval_child_run_timeout_s()
    if _cfg.run_timeout_seconds < _child_timeout:
        _cfg.run_timeout_seconds = _child_timeout
    # ... eval 运行 ...
finally:
    get_config().run_timeout_seconds = _original_run_timeout
```

或者在 `EvalSandbox` 里记录和恢复：

```python
class EvalSandbox:
    def apply(self):
        self._original_run_timeout = get_config().run_timeout_seconds
        # ... existing apply ...

    def restore(self):
        get_config().run_timeout_seconds = self._original_run_timeout
        # ... existing restore ...
```

EvalSandbox 方案更好——所有 suite 共享 restore 逻辑，不只 deepswe。

## 2. 验收

- eval 运行时 `run_timeout_seconds` 被修改
- eval 结束后 `run_timeout_seconds` 恢复原值
- 同一进程连续跑多个 suite，后续 suite 不继承前一个的修改
- `SHERRY_EVAL_CHILD_RUN_TIMEOUT_S` 手动覆盖仍生效，结束后恢复
