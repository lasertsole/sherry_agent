- 清理skills/builtin/code_wiki/SKILL.md 中关于 hermes 的标识
- 将skills/builtin/llm_wiki/SKILL.md内的所有中文 翻译 为英文
- 处理TODO/AUDIT_REPORT.md 的 19,20,27,38,44,45,48,50,51,53,57,58,59,61,63,68,70,73,75,处理后 冒烟、回归、e2e测试
- agent/middlewares/multimodal_processor.py 改名为 media_pipeline 并处理相关引用漂移，注释漂移和文档漂移
- 所有工具的description都要从 中文 改为 英文
- 前端历史消息列表 图片、视频、音频全需 懒加载

## runtime/ 重构：拆分 session/ + process/ 子包，Register → SessionRegister

### 目标

将 `runtime/` 从扁平结构拆分为 `runtime/session/`（会话级寄存器）和 `runtime/process/`（进程级服务），并将基类 `Register` 重命名为 `SessionRegister`，同时保留向后兼容别名。

### 文件迁移

| 旧路径                           | 新路径                                   | 说明                                                    |
| -------------------------------- | ---------------------------------------- | ------------------------------------------------------- |
| `runtime/core.py`                | `runtime/session/core.py`                | `SessionRegister` ABC + `clear_all_register_sessions()` |
| `runtime/state_register.py`      | `runtime/session/state_register.py`      | `StateRegisterMeM` + `StateRegisterDB`                  |
| `runtime/count_call_register.py` | `runtime/session/count_call_register.py` | `CountCallRegister`                                     |
| `runtime/relation_register.py`   | `runtime/session/relation_register.py`   | `RelationManager`                                       |
| `runtime/timer_call_register.py` | `runtime/session/timer_call_register.py` | `TimerCallRegister`                                     |
| `runtime/_callback_executor.py`  | `runtime/session/_callback_executor.py`  | 寄存器回调执行器                                        |
| `runtime/crash_loop_breaker.py`  | `runtime/process/crash_loop_breaker.py`  | 启动崩溃循环检测                                        |
| `runtime/periodic_backoff.py`    | `runtime/process/periodic_backoff.py`    | 周期退避服务                                            |

### 类重命名

| 旧类名              | 新类名                      | 说明                           |
| ------------------- | --------------------------- | ------------------------------ |
| `Register`          | `SessionRegister`           | ABC 基类，所有会话寄存器的父类 |
| `StateRegisterMeM`  | `StateRegisterMeM`（不变）  | 继承 `SessionRegister`         |
| `StateRegisterDB`   | `StateRegisterDB`（不变）   | 继承 `SessionRegister`         |
| `CountCallRegister` | `CountCallRegister`（不变） | 继承 `SessionRegister`         |
| `RelationManager`   | `RelationManager`（不变）   | 继承 `SessionRegister`         |
| `TimerCallRegister` | `TimerCallRegister`（不变） | 继承 `SessionRegister`         |

### 导入路径变更

**`runtime/__init__.py`（新旧对比）：**

旧代码：

```python
from .core import Register, clear_all_register_sessions
from .state_register import StateRegisterMeM, state_register_mem, StateRegisterDB, state_register_db
from .count_call_register import CountCallRegister, count_call_register
from .relation_register import RelationManager, relation_register
from .timer_call_register import TimerCallRegister, timer_call_register
from .crash_loop_breaker import record_boot, is_tripped, clear, mark_clean_exit, was_last_exit_clean
from .periodic_backoff import PeriodicBackoff
```

新代码：

```python
from .session.core import SessionRegister, clear_all_register_sessions
from .session.state_register import StateRegisterMeM, state_register_mem, StateRegisterDB, state_register_db
from .session.count_call_register import CountCallRegister, count_call_register
from .session.relation_register import RelationManager, relation_register
from .session.timer_call_register import TimerCallRegister, timer_call_register
from .process.crash_loop_breaker import record_boot, is_tripped, clear, mark_clean_exit, was_last_exit_clean
from .process.periodic_backoff import PeriodicBackoff

# 向后兼容别名：旧代码引用 ``runtime.Register``
Register = SessionRegister
```

**子包 `runtime/session/__init__.py`：**

```python
from .core import SessionRegister, clear_all_register_sessions
from .state_register import StateRegisterMeM, state_register_mem, StateRegisterDB, state_register_db
from .count_call_register import CountCallRegister, count_call_register
from .relation_register import RelationManager, relation_register
from .timer_call_register import TimerCallRegister, timer_call_register
```

**子包 `runtime/process/__init__.py`：**

```python
from .crash_loop_breaker import record_boot, is_tripped, clear, mark_clean_exit, was_last_exit_clean
from .periodic_backoff import PeriodicBackoff
```

### 全局导入替换规则

以下替换覆盖了约 38 个源文件和测试文件：

| 旧导入                                                           | 新导入                                                                          |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `from runtime.core import Register`                              | `from runtime.session.core import SessionRegister`                              |
| `from runtime.core import Register, clear_all_register_sessions` | `from runtime.session.core import SessionRegister, clear_all_register_sessions` |
| `from runtime.state_register import ...`                         | `from runtime.session.state_register import ...`                                |
| `from runtime.count_call_register import ...`                    | `from runtime.session.count_call_register import ...`                           |
| `from runtime.relation_register import ...`                      | `from runtime.session.relation_register import ...`                             |
| `from runtime.timer_call_register import ...`                    | `from runtime.session.timer_call_register import ...`                           |
| `from runtime.crash_loop_breaker import ...`                     | `from runtime.process.crash_loop_breaker import ...`                            |
| `from runtime.periodic_backoff import ...`                       | `from runtime.process.periodic_backoff import ...`                              |
| `from runtime import Register`                                   | `from runtime import SessionRegister`（或保持不变，因 `__init__.py` 有别名）    |
| `import runtime.core`                                            | `import runtime.session.core`                                                   |
| `import runtime.crash_loop_breaker as breaker`                   | `import runtime.process.crash_loop_breaker as breaker`                          |
| `import runtime.state_register`                                  | `import runtime.session.state_register`                                         |

### monkeypatch 字符串路径替换

| 旧字符串                                             | 新字符串                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------ |
| `"runtime.crash_loop_breaker.STATE_PATH"`            | `"runtime.process.crash_loop_breaker.STATE_PATH"`            |
| `"runtime.state_register.state_register_db.db_path"` | `"runtime.session.state_register.state_register_db.db_path"` |

### importlib 动态导入替换

| 旧代码                                              | 新代码                                                      |
| --------------------------------------------------- | ----------------------------------------------------------- |
| `importlib.import_module("runtime.state_register")` | `importlib.import_module("runtime.session.state_register")` |

### sys.modules 桩替换（tests/agent/tools/subagent/conftest.py）

旧代码：

```python
import runtime.core as _rc
_rc.Register._instances.clear()
```

新代码：

```python
import runtime.session.core as _rc
_rc.SessionRegister._instances.clear()
```

### 测试中 Register 类引用替换

| 旧代码                                 | 新代码                                        |
| -------------------------------------- | --------------------------------------------- |
| `Register._instances`                  | `SessionRegister._instances`                  |
| `Register.clear_all_register_sessions` | `SessionRegister.clear_all_register_sessions` |
| `(Register,)` （基类元组）             | `(SessionRegister,)`                          |

### 验证结果

- 1345 个测试通过（跨 `tests/agent/middlewares/`, `tests/runtime/`, `tests/server/`, `tests/bus/`, `tests/channels/`, `tests/skills/`）
- 3 个预存失败（与重构无关）：
  1. `test_increase_concurrent_no_lost_counts` — 线程安全 flaky 测试（198 vs 200）
  2. `test_spawn_respawns_when_pid_file_is_stale` — Windows 无 `true` 命令
  3. `test_clear_all_register_sessions_wipes_mem` — 测试顺序状态泄漏（单独运行通过）

### 附带修复：PowerShell Set-Content 编码损坏

PowerShell `Set-Content -NoNewline` 损坏了 27 个 `.py` 文件中的非 ASCII 字符。损坏模式为 3 字节 UTF-8 序列的第 3 字节被替换为 `?`（0x3f），以及相邻的 `"` 丢失。修复内容：

- 破折号 `—`（`\xe2\x80\x94`）→ `--`
- 右箭头 `→`（`\xe2\x86\x92`）→ `->`
- 水平线 `─`（`\xe2\x94\x80`）→ `-`
- 中文句号 `。`（`\xe3\x80\x82`）→ 恢复为 `。`
- 全角字符 `Ａ`/`Ｂ`/`Ｃ`（`\xef\xbc\xa1`/`\xa2`/`\xa3`）→ 逐个恢复
- 中文字符 `吗`/`是`/`否`/`无`/`字`/`啊` → 逐个恢复
- 丢失的 `"` 引号 → 手动补回
