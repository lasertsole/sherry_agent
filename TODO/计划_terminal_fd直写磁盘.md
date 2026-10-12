# 计划：terminal 子进程输出 fd 直写磁盘

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：sherry 的 `terminal.py` 用 `proc.communicate()` 把子进程 stdout **一次性全量收进 Python 内存**，再由 `ContextEvictionMiddleware` 判断是否驱逐到文件。超大输出（`cat` 大文件、`find / -type f`）先撑内存再驱逐——内存峰值 = 输出大小，可能 OOM

## 现状

### terminal.py 的两条输出路径

```python
# 同步路径（_run）
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
stdout, stderr = proc.communicate()  # ← 全量读进内存

# 异步路径（_arun）
proc = await asyncio.create_subprocess_exec(cmd, stdout=PIPE, stderr=PIPE)
stdout, stderr = await proc.communicate()  # ← 全量读进内存
```

### 后续驱逐（已有，但在全量收完后）

```
proc.communicate() 全量收完
  ↓
ContextEvictionMiddleware._maybe_evict(result)
  ↓ result > 20,000 字符？
  ├─ 是 → 写到 evicted/ 文件，替换为头 5 行 + 尾 5 行预览
  └─ 否 → 原样返回
```

### 内存问题

| 命令 | 输出大小 | 内存峰值（现在） | 内存峰值（fd 直写后） |
|---|---|---|---|
| `ls -la` | ~2KB | ~2KB | ~2KB（无差异） |
| `pip install -v` | ~500KB | ~500KB | ~5KB |
| `pytest -v`（1000 测试） | ~5MB | ~5MB | ~5KB |
| `cat bigfile.log` | ~500MB | **~500MB** ⚠️ | ~5KB |
| `find / -type f` | ~200MB | **~200MB** ⚠️ | ~5KB |

大多数 agent 命令输出 < 20,000 字符——无差异。但超大输出命令会让进程内存撑到几百 MB，可能触发 OOM 或让进程变慢。

## 设计：fd 直写

### 原理

把子进程的 stdout 文件描述符**直接指向磁盘文件**——子进程 `write()` 的数据由 OS 内核从管道刷到文件，Python 进程不 `read()` 这段数据。Python 只在子进程退出后读头尾几行做预览。

```
现在：  子进程 stdout → PIPE → Python 全量 read() → 内存 → 判断大小 → 驱逐到文件
fd直写：子进程 stdout → 文件（直接写磁盘）→ Python 只读头尾预览
```

### 配置

`config/features/agent_side/tools_timeouts.py` 新增：

```python
#: 输出超过此字节数时走 fd 直写驱逐路径（而非全量收内存）。
#: 设为 0 = 总是全量收内存（当前行为，向后兼容）。
#: 设为 None = 总是 fd 直写。
#: 默认 100_000（100 KB）——对齐 ContextEvictionMiddleware 的
#: evict_threshold_chars=20_000（CJK 20K chars ≈ 80KB），使两层阈值
#: 尽量重合，减少 20K chars~阈值 之间的输出被"先读进内存再驱逐"
#: 的双写浪费。
fd_write_threshold_bytes: int   # 默认 100_000（100 KB）
```

小于阈值的输出走现有路径（`communicate()` 全量收——小输出无内存问题，且 communicate 的 stdout 是 bytes，后续处理简单）。超过阈值走 fd 直写路径。

### 同步路径修改（`terminal.py` 的 `_run` / `_core`）

```python
import tempfile
import os

def _execute_command(cmd, cwd, timeout, session_id, *, fd_write_threshold=FD_WRITE_THRESHOLD):
    """执行命令。输出超阈值时 fd 直写磁盘，只回预览。"""
    # 小输出预判：先用 PIPE 启动，但通过 SO_RCVBUF 限制不行——
    # 实际方案：总是用临时文件做 stdout，但小文件直接读全文。

    with tempfile.NamedTemporaryFile(
        prefix=".sherry-stdout-", suffix=".log", delete=False, mode="wb"
    ) as tmp:
        tmp_path = Path(tmp.name)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=tmp,          # ← stdout fd 直接指向文件
                stderr=subprocess.PIPE,
                cwd=cwd,
                ...
            )
            _, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            ...

    # 读取文件大小判断走哪条路
    file_size = tmp_path.stat().st_size

    if file_size <= fd_write_threshold:
        # 小输出 → 全量读（和现在一样）
        stdout_text = tmp_path.read_bytes().decode(encoding, errors="replace")
        tmp_path.unlink()
        return stdout_text, stderr_text, exit_code

    # 大输出 → 移到 evicted/ 目录，只回预览
    return _evict_large_output(tmp_path, session_id, stderr_text, exit_code)
```

### 异步路径修改（`_arun`）

```python
async def _aexecute_command(cmd, cwd, timeout, session_id, ...):
    with tempfile.NamedTemporaryFile(..., mode="wb") as tmp:
        tmp_path = Path(tmp.name)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=tmp,           # ← stdout fd 直接指向文件
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            ...
        )
        _, stderr = await proc.communicate()  # stderr 还是 PIPE（通常很小）
        exit_code = proc.returncode

    file_size = tmp_path.stat().st_size
    if file_size <= fd_write_threshold:
        stdout_text = tmp_path.read_bytes().decode(encoding, errors="replace")
        tmp_path.unlink()
        return stdout_text, stderr_text, exit_code

    return _evict_large_output(tmp_path, session_id, stderr_text, exit_code)
```

### 驱逐逻辑（复用已有 evicted/ 机制 + `_PREVIEW_TEMPLATE`）

```python
def _evict_large_output(tmp_path: Path, session_id: str, stderr: str, exit_code: int):
    """把大输出移到 evicted/ 目录，返回预览 + 文件路径。

    全程不全量读文件——只读头 N 行 + 尾 N 行 + stat() 取大小。
    """
    from pub.func.message.eviction import get_eviction_dir
    from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION

    evict_dir = get_eviction_dir(session_id)  # SESSIONS_DIR/{session_id}/evicted/
    if evict_dir is None:
        # session_id 不安全 → 退化为全量读（和安全降级一致）
        stdout_text = tmp_path.read_bytes().decode("utf-8", errors="replace")
        tmp_path.unlink()
        return stdout_text, stderr, exit_code

    # 按 hash 命名（和现有 evict_tool_result 一致）
    with tmp_path.open("rb") as f:
        content_hash = hashlib.sha256(f.read(65536)).hexdigest()[:16]
    dest = evict_dir / f"terminal-{content_hash}.log"

    # 原子移动（同文件系统 mv 是 rename，O(1)）
    tmp_path.rename(dest)

    # 只读头尾 N 行做预览——不全量读
    head_n = TOOL_RESULT_EVICTION["preview_head_lines"]   # 5
    tail_n = TOOL_RESULT_EVICTION["preview_tail_lines"]   # 5
    head, tail = _read_head_tail(dest, head_n, tail_n)
    file_size = dest.stat().st_size

    # 用和 ContextEvictionMiddleware 一样的 _PREVIEW_TEMPLATE 格式
    stdout_text = (
        f"[evicted to: {dest}]\n"
        f"--- head ({head_n} lines) ---\n"
        f"{head}\n"
        f"...\n"
        f"--- tail ({tail_n} lines) ---\n"
        f"{tail}\n"
        f"[full content: {file_size:,} bytes, evicted at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')}]\n\n"
        f"Use read_file(file_path='{dest}', offset=0, limit=100) "
        f"to read the full content in chunks."
    )

    return stdout_text, stderr, exit_code


def _read_head_tail(path: Path, head_n: int, tail_n: int) -> tuple[str, str]:
    """读文件头 N 行 + 尾 N 行，不全量加载。

    头：从头迭代，读完 N 行即停。
    尾：seek 到末尾，按 8KB 块反向读直到拿到 N 行。
    全程内存 = head_n 行 + 8KB 块，与文件大小无关。
    """
    # ── 头 N 行 ──
    head_lines: list[str] = []
    with path.open("rb") as f:
        for i, line in enumerate(f):
            if i >= head_n:
                break
            head_lines.append(line.decode("utf-8", errors="replace").rstrip("\n"))
    head = "\n".join(head_lines)

    # ── 尾 N 行 ──
    tail_lines: list[str] = []
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        block_size = 8192
        buf = b""
        while pos > 0 and len(tail_lines) < tail_n:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
            tail_lines = buf.decode("utf-8", errors="replace").splitlines()
    tail = "\n".join(l for l in tail_lines[-tail_n:] if l.strip())

    return head, tail
```

**关键**：`_read_head_tail` **不调用 `_count_lines`**——旧设计里 `_count_lines(path)` 会遍历整个文件来算"省略了多少行"，这在 500MB 文件上等于把 fd 直写省掉的内存又读回来了。改用文件大小（`stat().st_size`，O(1)）标注总量，不数行数。

### stderr 处理

stderr 通常很小（错误消息、日志）——仍然走 PIPE + `communicate()` 全量收。只有 stdout 走 fd 直写。如果 stderr 也超大（如 `-v` 超长栈追踪），同样走 fd 直写路径。

```python
# 可选：stderr 也超阈值时直写
if len(stderr_bytes) > fd_write_threshold:
    stderr_text, stderr_path = _evict_large_output_bytes(stderr_bytes, session_id)
else:
    stderr_text = stderr_bytes.decode(encoding, errors="replace")
```

### read_file 预览路径

和现有的 `ContextEvictionMiddleware` 驱逐格式一致——模型看到预览 + 文件路径，用 `read_file(evicted_path, offset=..., limit=...)` 读全文。**模型不需要知道是 fd 直写还是驱逐后产生的——输出格式一样。**

### 与 ContextEvictionMiddleware 的关系

| 层 | 时机 | 阈值 | 做什么 |
|---|---|---|---|
| **fd 直写（新增）** | 命令执行时 | `fd_write_threshold_bytes` = 100 KB | stdout 直接写文件；超阈值时只回预览 |
| **ContextEvictionMiddleware（已有）** | 工具返回后 | `evict_threshold_chars` = 20,000 chars | 检查结果大小，超阈值驱逐到 evicted/ |

两层阈值刻意对齐（100 KB ≈ 20K CJK chars ≈ 80 KB），三档行为：

| 输出大小 | fd 直写 | ContextEvictionMiddleware | 结果 |
|---|---|---|---|
| **小**（< 20K chars / < 80 KB） | 文件 < 100 KB → 全量读返回 | < 20K chars → 不驱逐 | 全文进上下文，两层都不碰 ✅ |
| **中**（20K chars ~ 100 KB） | 文件 < 100 KB → 全量读返回 | > 20K chars → 驱逐到 evicted/ | fd 直写读进内存，ContextEviction 再驱逐 ⚠️ 双写 |
| **大**（> 100 KB） | 文件 > 100 KB → 直接驱逐 + 预览 | 预览 < 20K chars → 不驱逐 | fd 直写处理，ContextEviction 跳过 ✅ |

中间档（20K chars ~ 100 KB）有双写：fd 直写先把文件读进内存，ContextEviction 再把内存写回 evicted/ 文件。这档的内存峰值 = 输出大小（≤ 100 KB），不会 OOM，但多一次磁盘写。**可接受**——100 KB 的双写开销可忽略，且这是为"小输出不走驱逐、保持全文返回"付出的代价。

**结论**：fd 直写后，`ContextEvictionMiddleware` 对 terminal 工具只在中间档（20K chars ~ 100 KB）触发。大输出由 fd 直写处理，小输出两层都不碰。ContextEvictionMiddleware 仍然保护其他工具（python_repl、web_search 等不走 fd 直写的）。

## Q&A

**Q：terminal 输出较短，是不用驱逐吗？**

是。输出 < 100 KB（`fd_write_threshold_bytes`）→ fd 直写全量读返回全文 → ContextEvictionMiddleware 检查 < 20,000 chars → 不驱逐。两层都不碰，模型拿到完整输出。行为和现在（`communicate()` 全量收）一样。

**Q：terminal 输出较长，读取速度快吗？**

快。fd 直写驱逐时只做三件事：
1. `stat()` 取文件大小——O(1)
2. 从头迭代读 N 行（默认 5 行）——读到第 5 个 `\n` 就停，O(5 行)
3. `seek` 到末尾，按 8KB 块反向读直到拿到 N 行——通常 1 个 8KB 块就够（5 行 × 80 chars ≈ 400 bytes）

总内存 = 5 行 + 8KB 块，与文件大小无关。500MB 文件和 500KB 文件的预览读取时间一样（亚毫秒）。

旧设计有一个 bug：`_count_lines(path)` 会遍历整个文件来算"省略了多少行"，在 500MB 文件上等于把 fd 直写省掉的内存又读回来了。**已修复**——改用 `stat().st_size` 标注字节总量，不数行数。

**Q：fd 直写是无条件驱逐吗？**

不是。fd 直写**无条件把 stdout 写到临时文件**（`stdout=tmp`），但驱逐（只回预览 + 文件路径）是有条件的：

```
stdout → 临时文件（总是）
  ↓ stat() 文件大小
  ├─ ≤ 100 KB → 全量读返回（不驱逐，和现在一样）
  └─ > 100 KB → 移到 evicted/，只回预览（驱逐）
```

小输出：临时文件被读完后立即删除（`unlink`），模型拿到全文，不知道走过临时文件。
大输出：临时文件被 `rename` 到 evicted/ 目录，模型拿到预览 + 路径。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/tools/terminal.py` | 修改：_run / _arun 的 stdout 从 PIPE 改为临时文件 | ~80 改动 |
| `agent/tools/pub_base/` 或 `pub/func/message/` | 新增：`_evict_large_output()` + `_read_head_tail()`（合并了 tail 读取，无独立 `_read_tail_lines`）| ~50 |
| `config/features/agent_side/tools_timeouts.py` | 新增：`fd_write_threshold_bytes` 配置 | ~3 |
| `tests/agent/tools/test_terminal_fd_write.py` | 新建：fd 直写测试 | ~80 |

**合计 ~220 行**

## 验收

- 小输出命令（`echo hello`）→ 全量读内存，行为不变，临时文件删除
- 中等输出（20K chars ~ 100 KB）→ fd 直写全量读 → ContextEviction 驱逐（双写，可接受）
- 大输出命令（`cat bigfile`，500MB）→ stdout 直接写文件，Python 内存 ~5KB（`_read_head_tail` 不全量读）
- 预览格式和现有 ContextEvictionMiddleware 的 `_PREVIEW_TEMPLATE` 一致
- **预览读取不调用 `_count_lines`**——用 `stat().st_size` 标注字节总量，不遍历文件数行
- 模型能用 `read_file(evicted_path)` 读全文
- stderr 仍然全量收（通常小）
- 超时处理不变（proc.kill + 清理临时文件）
- 临时文件清理：正常退出删临时文件，移到 evicted/ 的不删
- 编码处理：`errors="replace"` 容错（和现有行为一致）

## 风险

- **临时文件清理**：进程崩溃 → 临时文件残留。缓解：临时文件用 `.sherry-stdout-` 前缀（和 `.sherry-tmp-` 原子写的临时文件一样的约定），下次启动时 `sweep_stale_temp_files` 清理（或加到 `sweep_orphaned_session_dirs` 里）
- **磁盘空间**：大输出写磁盘两份（临时文件 → mv 到 evicted/）。缓解：`rename` 是同文件系统 O(1) 操作，不复制。如果跨文件系统（temp 目录和 evicted/ 在不同 mount）才复制——确保临时文件在 session 目录同分区
- **编码**：二进制输出（`cat image.png`）写到文件后用 `errors="replace"` 读预览——和现有 `communicate()` + decode 行为一致
- **行缓冲**：子进程输出可能不 flush 到 fd → `proc.communicate()` 等待子进程退出后 fd 内容才完整。这是正确行为——fd 直写不需要流式预览，等子进程退出后一次读预览

## 不做的事

- **不做流式进度**——不做"命令还在跑时就实时读输出"。fd 直写是"等命令跑完后只读预览"，不是实时流。实时流需要 PTY 或异步流式读，复杂度高
- **不替换 ContextEvictionMiddleware**——fd 直写只管 terminal 工具的 stdout，其他工具（python_repl、web_search）仍由 ContextEvictionMiddleware 保护
- **不做输出大小预估**——不预先估算命令会产出多大输出。总是用临时文件做 stdout，事后按 `stat()` 文件大小决定读全文还是只读预览
- **不数行数**——预览不标注"省略了多少行"（旧设计调 `_count_lines` 遍历全文件，是 bug）。只标注文件字节数（`stat().st_size`，O(1)）
- **不改 stderr 路径**——stderr 通常小，保持 PIPE + communicate() 全量收。如果需要可后续加

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/terminal.py` | 修改：stdout PIPE → 临时文件 fd |
| `pub/func/message/eviction.py` | 参考：`get_eviction_dir()` / `build_preview()` |
| `config/features/agent_side/tools_timeouts.py` | 新增：`fd_write_threshold_bytes` |
| `config/features/agent_side/tool_result_eviction.py` | 参考：现有驱逐配置 |
| `tests/agent/tools/test_terminal_fd_write.py` | 新建：测试 |
