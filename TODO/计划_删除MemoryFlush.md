# 计划：删除 Memory Flush

- 建档日期：2026-10-23
- 状态：**计划（未开工）**
- **文档范围**：删除 Memory Flush 机制——便宜提取模型在压缩前从丢弃消息里机械追加事实到 MEMORY.md/USER.md。Memory Review（主模型、压缩时、编辑式维护）已覆盖全部能力且更优。Flush 在生产中实际未运行（`Summarization()` 构造时不传 `memory_store`/`llm_factory`，guard 永远 False）。
- 关联代码：见 §1 全量引用清单

---

## 0. 为什么删除

### 0.1 生产中已经不运行

`Summarization()` 在 `agent/core.py:388` 和 `agent/tools/subagent/spawn/core.py:1342` 实例化时**不传** `memory_store=` 和 `llm_factory=`——两个参数默认 `None`。compression.py 里的 flush guard `if self._memory_store and self._llm_factory:` 永远为 False。**Flush 只在测试里被显式调用。**

### 0.2 Memory Review 已覆盖全部能力

| 能力 | Memory Flush | Memory Review |
|---|---|---|
| 从丢弃消息抽取事实 | ✅ | ✅ |
| 写 MEMORY.md | ✅ 追加 | ✅ 编辑 |
| 写 USER.md | ✅ 追加 | ✅ 编辑 |
| 写 FACTS.md | ❌ | ✅ |
| 去重/合并 | ❌ | ✅ |
| 替换过时 | ❌ | ✅ |
| 剪枝噪声 | ❌ | ✅ "prefer nothing over noise" |
| 模型 | 便宜提取模型 | 主模型（cache-warm） |

Review 是 Flush 的超集——能做 Flush 能做的（抽取），还能做 Flush 做不到的（编辑、去重、写 FACTS.md）。Flush 的追加式写入还可能产生噪声（不去重、不合并）。

### 0.3 删除收益

- 删 3 个文件（~543 行）
- 简化 compression.py（删 2 段 flush 调用 + guard）
- 简化 Summarization 构造器（删 `memory_store`/`llm_factory` 死参数）
- 删 `MEMORY_FLUSH` 配置 + 环境变量
- 删 `append_entries` 的唯一生产调用方（方法本身保留——有独立单元测试）

---

## 1. 全量引用清单

### 1.1 删除的文件（3 个）

| 文件 | 行数 | 内容 |
|---|---|---|
| `agent/middlewares/summarization/memory_flush.py` | 180 | flush 实现（`_FLUSH_PROMPT`/`should_flush`/`run_memory_flush`/`run_memory_flush_sync`/`_render_discarded_text`/`_msg_to_text`/`_msg_type`） |
| `config/features/agent_side/memory_flush.py` | 39 | `MemoryFlushConfig` TypedDict + `MEMORY_FLUSH` 常量 |
| `tests/agent/middlewares/test_memory_flush.py` | 324 | flush 专用测试 |

### 1.2 要编辑的文件

| 文件 | 行 | 改动 |
|---|---|---|
| `agent/middlewares/summarization/compression.py` | 4-5, 10-11 | 删 docstring/注释里的 flush 引用 |
| `agent/middlewares/summarization/compression.py` | 255-265 | 删 sync 路径的 `from .memory_flush import run_memory_flush_sync` + 调用 + guard |
| `agent/middlewares/summarization/compression.py` | 348-358 | 删 async 路径的 `from .memory_flush import run_memory_flush` + 调用 + guard |
| `agent/middlewares/summarization/nudges.py` | **1112, 1114** | **关键**——`from ...memory_flush import _render_discarded_text` + 调用。删 memory_flush.py 后此 import 会 break。必须把 `_render_discarded_text` + `_msg_to_text` + `_msg_type` 搬到 nudges.py 或共享 util |
| `agent/middlewares/summarization/core.py` | 278-279, 288-289 | 删 `memory_store`/`llm_factory` 构造器参数 + 属性（flush 删除后变死参数） |
| `config/features/agent_side/__init__.py` | 136-139 | 删 `from .memory_flush import ...` |
| `config/features/__init__.py` | 44, 88 | 删 `MEMORY_FLUSH` / `MemoryFlushConfig` 导入 |
| `models/LLMs/compression_llm/core.py` | 4 | 删 docstring 里的 "run_memory_flush" |
| `agent/tools/memory.py` | 403, 423 | 改 `append_entries` 的 docstring/log（去掉 "Memory Flush" 标签） |
| `tests/agent/tools/taskflow/test_decision_model_seams.py` | 60 | 删路径 `"agent/middlewares/summarization/memory_flush.py"` |
| `tests/agent/tools/todolist/test_todolist_e2e.py` | 120, 567, 660-667 | 删 flush 调用步骤 |

### 1.3 文档更新

| 文件 | 改动 |
|---|---|
| `AGENTS.md` | 删 line 33 中间件列表的 flush 引用 + line 653 COMPRESSION_LLM doc + line 1418 |
| `.models.example` | 删 line 69 "memory_flush" |
| `README.md` + 四语言 | 删 COMPRESSION_LLM 表行的 flush 提及 |
| `agent/middlewares/README.md` + 四语言 | 删 file tree + tag-contract 段的 flush 提及 |
| `wiki/experience/README.md` + 四语言 | 删 flush 引用 |
| `wiki/long-running-tasks/README.md` + 四语言 | 删 flush 引用 |
| `wiki/long-running-tasks/memory/README.md` + 四语言 | 删 flush 引用 |
| `wiki/session_memory/README.md` + 四语言 | 删 flush 引用 |
| `client/app/i18n/locales/*.json` | 删 "memory flush" i18n 标签 |

### 1.4 `append_entries` 的处理

`MemoryStore.append_entries`（`memory.py:402`）的唯一生产调用方是 memory_flush。删除后：
- 生产代码不再调用 `append_entries`——变死代码
- 但有独立单元测试（`test_memory_store.py`、`test_memory_injection_guard.py`）直接测这个方法
- **保留 `append_entries` 方法 + 其单元测试**——它是 MemoryStore 的通用 API，未来可能有其他调用方。只改 docstring 去掉 "Memory Flush" 标签。

---

## 2. 关键风险：nudges.py 的隐藏依赖

`nudges.py:1112` 的 compression-todo-update nudge 从 memory_flush 模块 import `_render_discarded_text`：

```python
from agent.middlewares.summarization.memory_flush import _render_discarded_text
```

删 memory_flush.py 后这个 import 会 break。`_render_discarded_text` + 它依赖的 `_msg_to_text` + `_msg_type` 必须搬走。

**方案**：把这 3 个函数搬到 `nudges.py` 内部（私有函数）或 `pub/func/message/` 共享 util：

```python
# nudges.py 内部（最简单——只有 nudges 用）
def _render_discarded_text(discarded_messages):
    return "\n\n".join(f"[{_msg_type(m)}] {_msg_to_text(m)}" for m in discarded_messages)

def _msg_to_text(msg):
    # ... 从 memory_flush.py 搬过来

def _msg_type(msg):
    # ... 从 memory_flush.py 搬过来
```

这 3 个函数共 ~30 行，搬过来后 nudges.py 不再依赖 memory_flush 模块。

---

## 3. 改动清单

### 3.1 删除（3 文件，~543 行）

- [ ] 删 `agent/middlewares/summarization/memory_flush.py`
- [ ] 删 `config/features/agent_side/memory_flush.py`
- [ ] 删 `tests/agent/middlewares/test_memory_flush.py`

### 3.2 搬迁（~30 行）

- [ ] 把 `_render_discarded_text` + `_msg_to_text` + `_msg_type` 从 memory_flush.py 搬到 nudges.py 内部
- [ ] 改 nudges.py:1112 的 import 为内部调用

### 3.3 编辑 compression.py

- [ ] 删 sync 路径 flush 调用（lines 255-265）
- [ ] 删 async 路径 flush 调用（lines 348-358）
- [ ] 删 docstring/注释里的 flush 引用

### 3.4 编辑 summarization/core.py

- [ ] 删 `memory_store`/`llm_factory` 构造器参数 + 属性

### 3.5 编辑配置

- [ ] 删 `config/features/agent_side/__init__.py` 的 MEMORY_FLUSH 导入
- [ ] 删 `config/features/__init__.py` 的 MEMORY_FLUSH 导入

### 3.6 编辑其他

- [ ] `models/LLMs/compression_llm/core.py:4` — 删 docstring flush 引用
- [ ] `agent/tools/memory.py:403,423` — 改 append_entries docstring/log
- [ ] `tests/agent/tools/taskflow/test_decision_model_seams.py:60` — 删路径
- [ ] `tests/agent/tools/todolist/test_todolist_e2e.py` — 删 flush 步骤

### 3.7 文档

- [ ] AGENTS.md + README 四语言 + wiki 四语言 + client i18n — 删 flush 引用

---

## 4. 验收标准

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S1：memory_flush 模块不存在** | `import agent.middlewares.summarization.memory_flush` | ImportError |
| **S2：MEMORY_FLUSH 配置不存在** | `from config.features import MEMORY_FLUSH` | ImportError |
| **S3：compression 不调 flush** | grep `run_memory_flush` in compression.py | 0 matches |
| **S4：nudges 不 import memory_flush** | grep `from.*memory_flush import` in nudges.py | 0 matches |
| **S5：nudges 的 _render_discarded_text 仍工作** | compression-todo-update nudge 正常运行 | 不 break |
| **S6：Summarization 构造器无死参数** | `Summarization()` 无 `memory_store`/`llm_factory` 参数 | 构造正常 |
| **S7：append_entries 保留** | `MemoryStore.append_entries` 仍可调用 | 方法存在 + 单元测试通过 |
| **S8：现有测试不回归** | `pytest tests/agent/middlewares/test_summarization*.py tests/agent/tools/test_memory_store.py -q` | 全 pass |
| **S9：Memory Review 仍工作** | 压缩后 FACTS.md/MEMORY.md/USER.md 被正确维护 | nudge 正常运行 |
