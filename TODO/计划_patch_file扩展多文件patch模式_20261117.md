# 计划：patch_file 扩展多文件 patch 模式——不新建工具，扩展现有 patch_file

- 日期：2026-11-17（r3，合并到 patch_file，不新建工具）
- 状态：**待评审**
- 定位：给现有 `patch_file` 工具加一个 `patch` 参数——非空时进入多文件 patch 模式（Add/Update/Delete/Move），空时保持现有 str_replace 行为不变。
- 来源：Codex 的 apply_patch 格式 + Sherry 现有 patch_file 的全部基础设施（9 级匹配 + 原子写入 + CAS + HITL + evidence/LSP/claim）
- 风险等级：**低** — 扩展现有工具，零配置改动，现有行为不变
- 依赖：现有 `patch_file` 的全部基础设施

---

## -1. 汲取各家 agent 工具优点

全项目工具对比（Sherry / DSH / Codex / OpenClaw / ZCode / Hermes / Madorin / OpenCode / Claude Code / OpenCode）发现的优势，融入 patch_file patch 模式：

| 优点 | 来源 | 怎么融入 |
|------|------|---------|
| **多文件 patch 格式**（Add/Update/Delete/Move 一次调用） | Codex | patch 模式的格式用 Codex 兼容格式 |
| **4 级匹配**（exact → trimEnd → strip → Unicode 标点） | Codex | 作为基础——但 patch 模式实际**复用 patch_file 的 9 级匹配**（更强） |
| **auto-format 写后格式化** | OpenCode（`Format.Service`） | patch 模式写后复用 Sherry 的 autopep8（.py），未来可扩展 prettier/ruff |
| **hash CAS 预检** | Madorin（`expectedHash`） | preflight 用文件 revision CAS 校验"自上次 read_file 后未变"（复用 Sherry 的 fstat CAS） |
| **预检全过才写** | Codex | 三阶段：parse → preflight（内存模拟）→ commit |
| **多文件批量操作** | Madorin（`sys_write_files_batch`） | patch 模式本质就是批量操作——多文件一次调用 |
| **持久交互式 shell** | Madorin / DSH | 不涉及（patch 模式不调 terminal） |
| **原子写入 + revision CAS** | **Sherry（现有 write_file/patch_file）** | commit 阶段复用 `atomic_write_bytes_no_follow` + `file_write_lock` |
| **9 级模糊匹配** | **Sherry（现有 patch_file）** | Update File 的 hunk 匹配复用现有 9 级链 |
| **"Did you mean?" 提示** | **Sherry（现有 patch_file）** | hunk 不匹配时复用 `_find_closest_lines` |
| **read license 门控** | **Sherry（现有 write_file）** | Update File 要求先 read_file |
| **@invalidates_evidence** | **Sherry（现有）** | 装饰器自动标记 evidence 失效 |
| **LSP sync** | **Sherry（现有）** | 写后 `sync_edited_path` |
| **cross-agent claim notice** | **Sherry（现有）** | 写后 `_claim_notice` |
| **shadow_git checkpoint** | **Sherry（现有）** | `ensure_checkpoint`（per-turn dedup） |

### Sherry 在全项目工具对比中的优势（为什么 patch 模式基于 Sherry 而非其他）

| 维度 | Sherry 排名 | 说明 |
|------|-----------|------|
| Read token/堆安全 | **第 1** | 双重预算（token+行数，CJK 感知）+ 流式分块 + 逐行上限 + 全量读拒绝 |
| Write 原子性/CAS | **第 1** | atomic_write_bytes_no_follow + flock + fstat revision CAS |
| Edit 模糊匹配 | **第 1** | 9 级匹配链 + "Did you mean?" 提示 |
| Terminal 输出安全 | **第 1** | fd 直写文件 + eviction + head/tail |
| Search 后端韧性 | **第 1** | ripgrep + pure-Python fallback 双引擎 |
| 多文件 patch | **缺** | **← patch 模式补齐这个缺口** |
| auto-format | 第 2 | 只有 .py（OpenCode 全语言） |
| 选择语法 | 第 2 | Claude Code 的 GJC `:50-200` 更灵活 |

**结论**：Sherry 在单文件操作（read/write/edit/search）上已经是所有项目里最强的。唯一缺口是多文件 patch——Codex 和 OpenCode 有，Sherry 没有。patch 模式补这个缺口，用 Codex 的格式 + Sherry 自己的 9 级匹配/原子写入/CAS/全套集成——不需要从其他项目搬代码，只搬格式。

| 维度 | 新建 apply_patch | 扩展 patch_file |
|------|-----------------|----------------|
| 工具数 | 25（+1） | 24（不变） |
| prompt token | +~250（新工具描述） | +~80（描述加几行） |
| 配置改动 | 12 处 | **零** |
| HITL 门控 | 需加 | **已有** |
| parallel_safety | 需加 | **已有** |
| PTC allowlist | 需加 | **已有** |
| plan-mode 拒绝 | 需加 | **已有** |
| eval 断言 | 需更新 | **不需** |
| evidence/LSP/claim | 需装 | **已有** |
| LLM 发现性 | 需学习新工具 | **已知 patch_file** |

**12 处配置零改动 + 省 ~170 tokens/turn + LLM 零学习成本。**

---

## 1. 设计

### 1.1 args_schema 扩展

```python
class PatchFileInput(BaseModel):
    # 现有参数（str_replace 模式）
    file_path: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    # 新参数（patch 模式）
    patch: str | None = None
    # patch 非空 → 多文件模式（忽略 file_path/old_string/new_string）
    # patch 为空 → str_replace 模式（现有行为不变）
```

**模式判定**：`patch` 参数非空 → 多文件 patch 模式；`patch` 为空/None → 现有 str_replace 模式。

**现有调用不受影响**：
```python
# 现有调用（str_replace 模式）——完全不变
patch_file(file_path="x.py", old_string="a", new_string="b")

# 新调用（patch 模式）
patch_file(patch="*** Begin Patch\n*** Update File: x.py\n@@\n-a\n+b\n*** End Patch")
```

### 1.2 工具描述（改后）

```
Patch a file by replacing old_string with new_string. Uses fuzzy matching
to handle minor whitespace/indentation differences. Prefer this over
write_file for targeted edits.

For multi-file changes, provide a `patch` parameter instead: a patch block
starting with *** Begin Patch and ending with *** End Patch. Supports
*** Add File / *** Update File / *** Delete File / *** Move to.
Each Update File has @@ hunks with context/+added/-removed lines.
The whole patch is preflight-checked (syntax + hunk match + file existence)
before any file is written; a rejected patch changes nothing.
```

### 1.3 patch_file 入口分流

```python
@tool("patch_file", args_schema=PatchFileInput)
@invalidates_evidence
async def patch_file(file_path=None, old_string=None, new_string=None,
                     replace_all=False, patch=None, session_id=""):
    if patch:
        return await _apply_multi_file_patch(patch, session_id)
    else:
        # 现有 str_replace 逻辑（完全不变）
        return await _apply_str_replace(file_path, old_string, new_string,
                                         replace_all, session_id)
```

---

## 2. 多文件 patch 模式实现

### 2.1 Patch 格式（Codex 兼容）

```
*** Begin Patch
*** Add File: src/new.py
+import os
+def hello(): print("hello")
*** Update File: src/existing.py
@@
 def old():
-    return None
+    return "updated"
*** Delete File: src/old.py
*** Update File: src/moved.py
*** Move to: src/renamed.py
@@
-old
+new
*** End Patch
```

### 2.2 三阶段执行

```python
async def _apply_multi_file_patch(patch_text, session_id):
    workspace_root = session_workspace_root(session_id)

    # 阶段 1：语法解析
    try:
        operations = parse_patch(patch_text)
    except PatchParseError as e:
        return f"Patch syntax error at line {e.line}: {e.reason}"

    # 阶段 2：预检（复用现有 CAS + read license + 9 级匹配）
    refusals = preflight_patch(operations, session_id, workspace_root)
    if refusals:
        return f"Patch rejected ({len(refusals)} refusal(s)):\n" + \
               "\n".join(f"  - {r}" for r in refusals)

    # 阶段 3：提交（复用现有原子写入 + autopep8 + evidence + LSP + claim）
    ensure_checkpoint(session_id, reason="before_apply_patch")
    results = []
    for op in operations:
        result = await _commit_one_op(op, session_id, workspace_root)
        results.append(result)
        if result.failed:
            break  # 第一个失败停止，不回滚
    return _format_results(results)
```

### 2.3 预检（复用现有 CAS——不加 read-license）

**重要修正**：patch_file 当前**不要求 read license**（不像 write_file 有 `_overwrite_precondition`）。patch_file 只靠两层 CAS：read-stat fingerprint（`revision_id(check_stat)`）+ atomic write `expected_revision`。patch 模式保持一致——**不加 read-license 门控**，只用 CAS。

```python
def preflight_patch(operations, session_id, workspace_root):
    simulated = {}  # path → content（内存模拟）
    refusals = []

    for op in operations:
        path = workspace_root / op.path

        if op.type in ("update", "delete", "move"):
            if not path.exists():
                refusals.append(f"{op.type} {op.path}: file not found")
                continue
            # CAS：read-stat fingerprint（patch_file 现有机制，非 write_file 的 read-license）
            stat = path.stat()
            rev = revision_id(stat)  # 复用 patch_file.py:285-299 的 fingerprint
            # 预检只校验文件存在 + 能读 + hunk 能匹配，不校验 read-license

        if op.type == "add" and path.exists():
            refusals.append(f"add {op.path}: already exists, use Update File")
            continue

        if op.type in ("update", "move"):
            content = simulated.get(str(path)) or _read_text_no_follow(path)
            if content is None:
                refusals.append(f"{op.type} {op.path}: binary file")
                continue
            for hunk in op.hunks:
                # 复用 fuzzy_find_and_replace（真名，非 _fuzzy_match）
                result = _apply_hunk_with_fuzzy_match(content, hunk)
                if not result.ok:
                    # 复用 _find_closest_lines（patch_file.py:72 本地函数）
                    closest = _find_closest_lines(content, hunk.old_lines)
                    refusals.append(f"hunk mismatch in {op.path}: {closest}")
                    break
            else:
                simulated[str(path)] = result.content

    return refusals
```

**关键修正**：
- 不用 `_has_read_license`（不存在）——不用 `_overwrite_precondition`（write_file 专属）
- 用 `revision_id(check_stat)` CAS（patch_file 现有机制）
- `_fuzzy_match` → `fuzzy_find_and_replace`（`pub_base/text_matcher.py:468`，通过 `pub_base` re-export 导入）
- `_find_closest_lines` 是 patch_file.py 本地函数（line 72）——patch 模式在同文件内直接调

### 2.4 提交（复用现有原子写入——修正符号名）

```python
async def _commit_one_op(op, session_id, workspace_root):
    path = workspace_root / op.path
    try:
        if op.type == "add":
            content = "\n".join(op.content_lines)
            if path.suffix == ".py":
                content = _format_py_code(content)  # write_file.py:78 本地函数
            with file_write_lock(str(path)):  # pub_base/file_lock.py:144
                atomic_write_bytes_no_follow(path, content.encode("utf-8"))  # pub_base/atomic_write.py:382
            note_edit(session_id, path)  # pub_base/read_state.py:102（非 _note_edit）
            sync_edited_file(str(path), content, session_id)  # pub_base/lsp_sync.py:77（非 sync_edited_path）
            # claim notice：复用 patch_file.py:221-225 的内联形式
            _inline_claim_notice(session_id, path)

        elif op.type == "update":
            old = _read_text_no_follow(path)
            new = _apply_hunks_with_fuzzy_match(old, op.hunks)  # fuzzy_find_and_replace
            if path.suffix == ".py":
                new = _format_py_code(new)
            stat = path.stat()
            rev = revision_id(stat)  # CAS fingerprint
            with file_write_lock(str(path)):
                atomic_write_bytes_no_follow(path, new.encode("utf-8"), expected_revision=rev)
            note_edit(session_id, path)
            sync_edited_file(str(path), new, session_id)
            _inline_claim_notice(session_id, path)

        elif op.type == "delete":
            with file_write_lock(str(path)):
                path.unlink()
            note_edit(session_id, path)

        elif op.type == "move":
            old = _read_text_no_follow(path)
            new = _apply_hunks_with_fuzzy_match(old, op.hunks)
            dest = workspace_root / op.dest_path
            if dest.suffix == ".py":
                new = _format_py_code(new)
            dest_stat = dest.stat() if dest.exists() else None
            dest_rev = revision_id(dest_stat) if dest_stat else "absent"
            with file_write_lock(str(dest)):
                atomic_write_bytes_no_follow(dest, new.encode("utf-8"), expected_revision=dest_rev)
            with file_write_lock(str(path)):
                path.unlink()
            note_edit(session_id, path)
            note_edit(session_id, dest)
            sync_edited_file(str(dest), new, session_id)
            _inline_claim_notice(session_id, path)
            _inline_claim_notice(session_id, dest)

        return OpResult(op, "completed")
    except Exception as e:
        return OpResult(op, "failed", str(e))
```

**符号名修正**：
- `note_edit`（非 `_note_edit`）—— `pub_base/read_state.py:102`
- `sync_edited_file`（非 `sync_edited_path`）—— patch_file 用 `sync_edited_file`（content 在手），write_file 用 `sync_edited_path`（重读文件）。patch 模式有 content → 用 `sync_edited_file`
- `_inline_claim_notice` —— 复用 patch_file.py:221-225 的内联 `claim_notice_for`（非 write_file 的 `_claim_notice`）
- `fuzzy_find_and_replace`（非 `_fuzzy_match`）—— `pub_base/text_matcher.py:468`
- `revision_id(stat)` —— patch_file 现有 CAS fingerprint（非 write_file 的 `_overwrite_precondition` read-license）
- `atomic_write_bytes_no_follow` 带 `expected_revision` —— 和 patch_file 现有两层 CAS 一致

---

## 3. 全条件推演——是否损坏已有功能

### 3.1 现有 str_replace 模式

| # | 场景 | 预期 | 是否受影响 |
|---|------|------|-----------|
| 1 | agent 调 patch_file(file_path="x.py", old_string="a", new_string="b") | str_replace 模式，现有行为 | ❌ 不受影响（patch=None → 走 _apply_str_replace） |
| 2 | agent 调 patch_file(file_path="x.py", old_string="a", new_string="b", replace_all=True) | 全量替换 | ❌ 不受影响 |
| 3 | 现有 patch_file 测试 | 全过 | ❌ patch=None → 现有路径 |
| 4 | HITL first_call_confirmation | patch_file 在列表 → 弹审批 | ❌ 不受影响（工具名不变） |
| 5 | parallel_safety FILE_WRITE_TOOLS | patch_file 在列表 → 序列化 | ❌ 不受影响 |
| 6 | plan-mode 拒绝 | patch_file 被拒 | ❌ 不受影响 |
| 7 | PTC allowed_tools | patch_file 在白名单 | ❌ 不受影响 |
| 8 | tool_result_eviction | patch_file 结果不被 evict | ❌ 不受影响 |
| 9 | @invalidates_evidence | 装饰器在 patch_file 上 → 自动标记 | ❌ 不受影响（装饰器在函数级，两种模式都标记） |
| 10 | clears_guardrail_blocks metadata | patch_file 有 → 清除 guardrail block | ❌ 不受影响（metadata 在工具级） |
| 11 | eval harness 工具集断言 | patch_file 在断言集 | ❌ 不受影响（工具名不变） |
| 12 | shadow_git checkpoint | patch_file 调 ensure_checkpoint | ❌ 不受影响（两种模式都调） |
| 13 | tool schema golden test | WRITE_FILE_SCHEMA 冻结 | ⚠️ **需检查**——如果 golden test 检查 patch_file 的 args_schema 字段，加 `patch` 参数会破坏 |

### 3.2 新 patch 模式 + 现有功能交互

| # | 场景 | 预期 | 风险 |
|---|------|------|------|
| 14 | patch 模式 + HITL confirm_all | patch_file 在 first_call_confirmation → 弹审批 | ✅ 自动继承（工具名不变） |
| 15 | patch 模式 + parallel_safety | patch_file 在 FILE_WRITE_TOOLS → 同文件并发序列化 | ✅ 自动继承 |
| 16 | patch 模式 + plan-mode | patch_file 被拒 | ✅ 自动继承 |
| 17 | patch 模式 + @invalidates_evidence | 装饰器自动标记 evidence | ✅ 自动继承（函数级装饰器） |
| 18 | patch 模式 + shadow_git | ensure_checkpoint 一次（per-turn dedup） | ✅ 复用现有 |
| 19 | patch 模式 + read license CAS | Update File 要求先 read_file | ✅ preflight 复用 _has_read_license |
| 20 | patch 模式 + 9 级匹配 | hunk 匹配用 9 级 | ✅ 复用 _fuzzy_match |
| 21 | patch 模式 + "Did you mean?" | hunk 不匹配给最近行提示 | ✅ 复用 _find_closest_lines |
| 22 | patch 模式 + atomic_write | 每个 op 用 atomic_write_bytes_no_follow | ✅ 复用 |
| 23 | patch 模式 + file_write_lock | 每个 op 加锁 | ✅ 复用 |
| 24 | patch 模式 + autopep8 | .py 文件写后格式化 | ✅ 复用 _format_py_code |
| 25 | patch 模式 + LSP sync | 写后 sync_edited_path | ✅ 复用 |
| 26 | patch 模式 + claim notice | 写后 _claim_notice | ✅ 复用 |
| 27 | patch 模式 + eval orchestration gate | patch_file 在 _ORCHESTRATION_GATED → gated | ✅ 自动继承 |
| 28 | patch 模式 + tool_result_eviction | patch_file 在 excluded_tools → 不 evict | ✅ 自动继承 |
| 29 | patch 模式 + redaction | patch_file 在豁免集 | ✅ 自动继承 |

### 3.3 潜在风险点

| # | 风险 | 严重性 | 缓解 |
|---|------|--------|------|
| 30 | **args_schema golden test** | 中 | 检查 `tests/agent/tools/test_tool_schema_goldens.py`——如果冻结了 patch_file 的 schema 字段，加 `patch` 参数会破坏。**需检查** |
| 31 | **LLM 同时传 patch + file_path** | 低 | patch 非空时忽略 file_path/old_string/new_string（在 _apply_multi_file_patch 入口丢弃） |
| 32 | **@invalidates_evidence 对 patch 模式的多文件** | 低 | 装饰器在函数级——patch 模式改 5 个文件 = 装饰器触发 1 次（函数调用 1 次）。evidence 标记的是"有文件被编辑"而非具体哪个文件——和 str_replace 模式行为一致 |
| 33 | **_note_edit 在 patch 模式调多次** | 低 | str_replace 模式调 1 次 _note_edit（1 个文件）；patch 模式调 N 次（N 个文件）。但 _note_edit 只是记录到 verification_state——多次调用是正确的（每个文件都该记录） |
| 34 | **sync_edited_path 在 patch 模式调多次** | 低 | 同上——每个编辑的文件都该 LSP sync |
| 35 | **_claim_notice 在 patch 模式调多次** | 低 | 同上——每个编辑的文件都该通知 |
| 36 | **file_write_lock 在 patch 模式——Move 操作锁两个文件** | 低 | 先锁目标写，再锁源删——两次独立加锁，不死锁（不同文件） |
| 37 | **ensure_checkpoint 在 patch 模式只调 1 次** | ✅ | 在 _apply_multi_file_patch 开头调一次，_commit_one_op 不调（per-turn dedup 保证只 1 次 snapshot） |
| 38 | **preflight 的 _has_read_license 和 str_replace 模式的 _overwrite_precondition 是否一致** | 中 | 需确认 _has_read_license 和 _overwrite_precondition 是同一套 read license 机制。如果是同一套 → 兼容；如果不同 → 需统一 |

### 3.4 代码验证结果（已检查）

- [x] `tests/agent/tools/test_tool_schema_goldens.py`——patch_file 的 schema **未被冻结**（只冻结 write_file/terminal/python_repl）→ 加 `patch` 参数安全 ✅
- [x] `_has_read_license` vs `_overwrite_precondition`——`_has_read_license` **不存在**；`_overwrite_precondition` 是 write_file 专属。patch_file **不用 read-license**，只用 CAS（`revision_id(check_stat)`）。patch 模式保持一致——**不加 read-license** ✅
- [x] `@invalidates_evidence` 多文件——函数级装饰器，patch 模式改 N 文件 = 触发 1 次（和 str_replace 一样）✅
- [x] `note_edit`（非 `_note_edit`）多次调用——str_replace 调 1 次，patch 模式调 N 次——正确（每文件都该记录）✅
- [x] `sync_edited_file`（非 `sync_edited_path`）多次调用——patch_file 用 `sync_edited_file`（content 在手），patch 模式同。每文件都该 LSP sync ✅
- [x] `_claim_notice` 不可导入——patch_file 已内联 `claim_notice_for`（line 221-225），patch 模式复用内联形式 ✅
- [x] `clears_guardrail_blocks: True`——patch_file metadata 有此标记（write_file 没有），patch 模式继承 ✅
- [x] `fuzzy_find_and_replace`（非 `_fuzzy_match`）——`pub_base/text_matcher.py:468`，通过 `pub_base` re-export 导入 ✅
- [x] `_find_closest_lines`——patch_file.py:72 本地函数，patch 模式在同文件内直接调 ✅
- [x] LSP sync 函数——patch_file 用 `sync_edited_file`（content 在手），**不用** `sync_edited_path`（write_file 用这个因为要重读）✅

---

## 4. 文件变更清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/tools/file_tools/patch_file.py` | 扩展：加 `patch` 参数到 PatchFileInput + `_apply_multi_file_patch` + `_commit_one_op` + 工具描述更新 | ~200 |
| `agent/tools/file_tools/patch_parser.py` | 新建：patch 格式解析器（parse_patch + PatchOperation + Hunk 类型） | ~200 |
| `agent/tools/file_tools/patch_preflight.py` | 新建：预检模拟器（复用 read license + CAS + 9 级匹配） | ~150 |
| `tests/agent/tools/file_tools/test_patch_parser.py` | 解析器测试 | ~150 |
| `tests/agent/tools/file_tools/test_patch_file_patch_mode.py` | patch 模式端到端测试 | ~200 |
| **总计** | | **~900** |

**零配置改动**——不碰 hitl_defaults / parallel_safety / ptc / summarization / tool_result_eviction / plan_mode / eval / redaction。

---

## 5. 验收

### str_replace 模式回归（不受影响）

| # | 场景 | 预期 |
|---|------|------|
| 1 | patch_file(file_path, old_string, new_string) | 现有行为不变 |
| 2 | patch_file(file_path, old_string, new_string, replace_all=True) | 现有行为不变 |
| 3 | 现有 patch_file 全部测试 | 全过 |
| 4 | HITL/parallel_safety/plan-mode/PTC/eval | 全部不变 |

### patch 模式新功能

| # | 场景 | 预期 |
|---|------|------|
| 5 | patch_file(patch="*** Begin Patch...") 单文件 Add | 创建文件（含 autopep8 if .py） |
| 6 | patch 模式单文件 Update（1 hunk） | 9 级匹配替换 |
| 7 | patch 模式单文件 Update（多 @@ hunk） | 多处替换 |
| 8 | patch 模式多文件（Add + Update + Delete + Move） | 全部原子提交 |
| 9 | 语法错误 | ParseError → 不改任何文件 |
| 10 | hunk 不匹配 | 预检失败 + "Did you mean?" → 不改任何文件 |
| 11 | 文件不存在 | 预检失败 |
| 12 | Add File 覆盖已有 | 预检失败 → "use Update File" |
| 13 | Update 未先 read_file | 预检失败 → "must read_file first" |
| 14 | 文件自读取后变更 | 预检失败 → "stale, re-read" |
| 15 | 提交时第 3 个 op 失败 | 前 2 个已写，第 3 failed，第 4+ skipped |

### 复用验证

| # | 场景 | 预期 |
|---|------|------|
| 16 | patch 模式用 atomic_write_bytes_no_follow | temp+rename（非 path.write_text） |
| 17 | patch 模式用 file_write_lock | 跨进程 flock |
| 18 | .py 文件 patch 模式写后 autopep8 | 格式化生效 |
| 19 | patch 模式后 @invalidates_evidence | evidence 标记失效 |
| 20 | patch 模式后 sync_edited_path | LSP sync |
| 21 | patch 模式后 _claim_notice | cross-agent 通知 |
| 22 | patch 模式 hunk 匹配用 9 级 | smart dash/quote/indentation 匹配 |
| 23 | patch 模式 hunk 不匹配有 "Did you mean?" | 最近行提示 |
| 24 | patch 模式 shadow_git 1 次 checkpoint | per-turn dedup |

### 兼容验证

| # | 场景 | 预期 |
|---|------|------|
| 25 | HITL confirm_all + patch 模式 | 弹审批（patch_file 在确认列表） |
| 26 | parallel_safety + patch 模式 | 同文件并发序列化 |
| 27 | plan-mode + patch 模式 | 被拒绝 |
| 28 | PTC + patch 模式 | 在 EXECUTOR 白名单 |
| 29 | tool_result_eviction + patch 模式 | 结果不被 evict |
| 30 | eval orchestration gate + patch 模式 | gated（需先 todowrite + taskflow） |
| 31 | write_file/patch_file str_replace 仍正常 | 共存无冲突 |
| 32 | 子 agent 有 patch_file（含 patch 模式） | scope 不设 → both |
| 33 | args_schema golden test | **需检查**——如果冻结了 patch_file schema |

---

## 6. Kill criteria

- ~~如果 args_schema golden test 冻结了 patch_file 的字段集~~ → 已验证：patch_file 不在 golden test 里，加 `patch` 参数安全 ✅
- ~~如果 `_has_read_license` 和 `_overwrite_precondition` 不是同一套机制~~ → 已验证：`_has_read_license` 不存在，patch_file 不用 read-license，patch 模式用 CAS-only（和 str_replace 一致）✅
- 如果 9 级匹配在多文件场景下太慢（N 文件 × M hunk × 9 级），降级为前 4 级（保留 9 级给 str_replace 模式）
- 如果预检模拟器在大 patch（100+ 操作）下太慢，加预检超时（10s）+ 超时跳过预检
- 如果 agent 不使用 patch 模式而继续用 str_replace + write_file 逐文件改，加 system prompt 引导
- 如果 autopep8 在多文件 patch 中导致 hunk 偏移（格式化改变行号），改为写后统一格式化
