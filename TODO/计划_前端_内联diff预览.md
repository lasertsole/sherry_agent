# 计划：前端内联编辑 diff 预览

- 建档日期：2026-10-23
- 状态：**待评审**
- **文档范围**：在 sherry 工具卡片（`ChatToolCard.vue`）上把 `patch_file` 的结果从裸 JSON `<pre>` 改为彩色 diff 预览（增行绿、删行红、上下文灰），让文件编辑可读性大幅提升。**patch_file 后端已返回 unified diff，纯前端解析渲染，零后端改动。**
- **硬约束**：不破坏现有工具卡片渲染；非 patch_file 工具退化原 `<pre>`；diff 渲染 fail-open。
- 关联代码：
  - sherry 改造对象：
    - `client/app/components/chat/ChatToolCard.vue:41-52,125-131`（args/result `<pre>` 渲染 + `formatToolArgs`）
    - `client/app/composables/message-items.ts:86-110,105`（`toolResult` 来源——tool row content 字段）
    - `client/app/composables/use-stream-chunks.ts:293-326,315`（流式 `tool_result` handler——`toolResult = content`）
    - `client/app/pages/home/type.ts:87`（`MessageItem.toolResult: string`）
  - sherry 后端（已就绪，复用）：
    - `agent/tools/file_tools/patch_file.py:57-64,195-204`（`_unified_diff` + 返回 JSON 带 `"diff"` 字段）
    - `agent/tools/file_tools/write_file.py:211`（返回纯成功字符串——无 diff）
  - ZCode 参考：
    - `ZCode-main/.../packages/ui/src/components/ui/lightweight-diff-preview.tsx`（纯前缀分类 + 行着色）
    - `ZCode-main/.../packages/ui/src/lib/patchDiffPreview.ts:296-345`（`@@`/`---`/`+++` 头剥离逻辑）

---

## 0. 背景（已核实）

### 0.1 patch_file 后端已返回 unified diff

`patch_file.py:195-204` 的 `_core` 返回 JSON 字符串：

```python
return json.dumps({
    "success": True, "path": display_path(...), "strategy": strategy,
    "matches": match_count, "diff": diff
}, ensure_ascii=False)
```

其中 `diff` 由 `_unified_diff`（`:57-64`）生成——Python 标准库 `difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile="a/{path}", tofile="b/{path}")`。**diff 已经算好了，客户端只需解析 JSON 取 `.diff` 字段。**

### 0.2 当前渲染

`ChatToolCard.vue:47-52` 渲染结果：`<pre>{{ message.toolResult }}</pre>`——把整段 JSON 字符串（含转义的 `\n`-填充的 diff）原样显示为纯文本。用户看到的是一坨 JSON，不是可读的 diff。

### 0.3 write_file 无 diff

`write_file.py:211` 返回纯字符串 `"File written successfully to {path}."`，**不返回 diff 也不返回旧内容**。write_file 的 diff 预览需要后端改动（在返回里加 `diff`/`old_content`），或从 `toolArgs.text`（新内容）做纯"新增文件"预览。

### 0.4 ZCode 做法

ZCode 的 `lightweight-diff-preview.tsx` 用**纯前缀分类**（不依赖 diff 库）：
- `getLightweightDiffLineParts(line)`（`:37-71`）按行首字符分类：`+`→`added`，`-`→`removed`，空格→`context`；标记字符从显示中剥离（`getPatchPreviewLineContent` 切 `[1:]`）
- `getLightweightDiffLineStyles`（`:73-111`）行着色：added=绿底+左边框+gutter，removed=红底，context=纯文本
- `patchDiffPreview.ts:296-345` 的 `collectPlainTextPreviewLines` 跳过 `@@`/`---`/`+++` 头，只保留 `+`/`-`/空格前缀的正文行
- 可选叠加 Shiki 异步高亮（第一帧纯文本，shiki token 后补丁式注入）

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：不破坏现有渲染 | 非 patch_file 工具仍走原 `<pre>` 渲染 | 其他工具卡片 byte-identical |
| **F2**：fail-open | JSON 解析失败/diff 为空/异常时退化原 `<pre>` | 解析失败不崩 |
| **F3**：零后端改动 | patch_file 的 diff 已有，直接消费 | 后端零改动 |
| **F4**：流式兼容 | `tool_result` 到达时即时渲染 diff | 工具结果到达即可见 diff |

---

## 2. 任务一：diff 解析 + 轻量渲染器

### 2.1 目标

解析 patch_file 的 JSON 结果，提取 unified diff，用纯前缀分类渲染彩色 diff。

### 2.2 改动清单

- [ ] **2.2.1** 新建 `client/app/composables/use-tool-diff.ts`：
  ```typescript
  interface DiffLine {
    type: 'added' | 'removed' | 'context' | 'hunk'  // +, -, 空格, @@
    content: string  // 去除前缀字符后的内容
    rawPrefix: string  // 原始前缀（+/−/空格），用于 gutter
    oldLineNo?: number  // 旧行号
    newLineNo?: number  // 新行号
  }
  ```
  - `parsePatchFileResult(toolResult: string): DiffLine[] | null`：
    - `JSON.parse(toolResult)` → 读 `.diff`（fail-open：解析失败返回 null）
    - 按 `\n` 拆分 diff → 跳过 `---`/`+++` 头 → 跳过 `@@` hunk 头（或保留为 `hunk` 类型）→ 其余按行首 `+`/`-`/空格 分类
    - 行号从 `@@ -oldStart,oldCount +newStart,newCount @@` 解析（可选，第一版可不做行号）
  - `isPatchFileResult(toolName, toolResult)`：判断是否是可渲染 diff 的结果（`toolName === 'patch_file'` && JSON 有 `.diff`）

- [ ] **2.2.2** 新建 `client/app/components/chat/DiffPreview.vue`：
  - 接收 `lines: DiffLine[]` prop
  - 每行渲染为 `<div>` + 行首 gutter（`+/−`标记）+ 内容
  - CSS：`.diff-added`（绿底 `#e6ffec`/dark `#1a3a2e` + 左边框绿 + gutter 绿），`.diff-removed`（红底 `#ffebe9`/dark `#3a1a1e` + 左边框红 + gutter 红），`.diff-context`（无背景），`.diff-hunk`（蓝灰底 + 等宽字体）
  - 可选：行号 gutter（左列旧行号/右列新行号）
  - 可选：与 §2.1 代码高亮联动——diff 行内容叠加 Shiki 高亮（延迟到 settle，同代码高亮计划）

- [ ] **2.2.3** `ChatToolCard.vue:47-52` 改造结果渲染区：
  ```vue
  <!-- 结果区 -->
  <template v-if="diffLines">
    <DiffPreview :lines="diffLines" />
  </template>
  <template v-else>
    <pre>{{ message.toolResult }}</pre>
  </template>
  ```
  - `diffLines` computed：`isPatchFileResult(message.toolName, message.toolResult) ? parsePatchFileResult(message.toolResult) : null`
  - fail-open：`diffLines` 为 null 时走原 `<pre>`

**验收**：F1 + F2 + F3 + F4。

---

## 3. 任务二：write_file diff 预览（需后端小改）

### 3.1 目标

write_file 也显示 diff（当前只返回成功字符串，无 diff/旧内容）。

### 3.2 改动清单

- [ ] **3.2.1** 后端 `write_file.py:211` 改为返回 JSON（对齐 patch_file 格式）：
  ```python
  old_content = read_file(resolved) if exists else ""
  diff = _unified_diff(old_content, text, resolved) if old_content else None
  return json.dumps({"success": True, "path": display_path(...), "diff": diff, "isNew": old_content == ""}, ensure_ascii=False)
  ```
  - 新建文件：`diff = null`，`isNew = true`——前端显示"新建文件"标记 + 新内容预览（全绿）
  - 覆盖文件：`diff` 有值——走 `DiffPreview`
  - append：diff 可能很大，只显示尾部追加部分

- [ ] **3.2.2** `use-tool-diff.ts` 增 `parseWriteFileResult`：同 patch_file 逻辑，读 `.diff`

- [ ] **3.2.3** `DiffPreview.vue` 增"新建文件"模式：`diff = null` + `isNew = true` → 全部行绿色（新增）

### 3.3 风险

- **F1 向后兼容**：write_file 返回格式从纯字符串变 JSON——老前端版本会把 JSON 原样显示在 `<pre>`（不崩，只是难看）。**可选**：用环境变量/feature flag 控制，或接受一次性切换。

**验收**：F2 + F4。新建 `tests/agent/tools/file_tools/test_write_file_diff.py`。

---

## 4. 任务三：（可选）Shiki 叠加高亮

### 4.1 目标

diff 行内容叠加语法高亮（绿色行里的代码也高亮），与代码高亮计划联动。

### 4.2 改动清单

- [ ] **4.2.1** `DiffPreview.vue`：每行内容调 `use-shiki.ts` 的 `highlightCode(content, lang)`
- [ ] **4.2.2** 语言推断：从 `toolArgs.file_path` 扩展名推断（`.py`→python, `.ts`→typescript）
- [ ] **4.2.3** 异步高亮：第一帧纯文本，Shiki token 后补丁式注入（同 ZCode `highlighted-lightweight-diff-preview.tsx` 模式）

**验收**：与代码高亮计划联合验收。

---

## 5. 执行顺序

```
§2 diff 解析 + 轻量渲染器（1.5 天）  ← patch_file 零后端改动
  ├─ use-tool-diff.ts
  ├─ DiffPreview.vue
  └─ ChatToolCard.vue 改造
      │
      ↓
§3 write_file diff（1 天，需后端小改）
  ├─ write_file.py 返回 JSON + diff
  ├─ use-tool-diff 增 write_file 分支
  └─ DiffPreview 增"新建文件"模式
      │
      ↓ （依赖代码高亮计划）
§4 Shiki 叠加高亮（0.5 天，可选）

总工程量：1.5 天（仅 patch_file）/ 2.5 天（含 write_file）/ 3 天（含 Shiki）
```

**关键**：§2 不依赖代码高亮计划——diff 渲染本身不依赖 Shiki，只是彩色行；Shiki 是可选增强。

---

## 6. 验收标准

- [ ] **F1**：非 patch_file 工具仍走原 `<pre>`。
- [ ] **F2**：JSON 解析失败退化 `<pre>`。
- [ ] **F3**：patch_file 零后端改动。
- [ ] **F4**：工具结果到达即可见 diff。
- [ ] patch_file 结果显示彩色 diff：增行绿、删行红、上下文灰。
- [ ] `@@` hunk 头显示为蓝灰分隔行。
- [ ] （可选）write_file 新建/覆盖也显示 diff。
- [ ] （可选）diff 行内容 Shiki 高亮。

---

## 7. 不做的事

| 项 | 不做的原因 |
|---|---|
| 引入 diff 库（@pierre/diffs 等） | patch_file 已返回 unified diff，纯前缀分类足够，无额外依赖 |
| side-by-side 双栏 diff | 第一版做 inline 单栏 diff（轻量）；双栏后续 |
| 代码高亮作为前置依赖 | diff 渲染独立可用，Shiki 是可选增强 |

---

## 8. 关键参考

### sherry 内部
- `client/app/components/chat/ChatToolCard.vue:41-52,125-131`（渲染区 + formatToolArgs）
- `client/app/composables/message-items.ts:86-110,105`（toolResult 来源）
- `client/app/composables/use-stream-chunks.ts:293-326,315`（流式 tool_result）
- `client/app/pages/home/type.ts:87`（MessageItem.toolResult 类型）
- `agent/tools/file_tools/patch_file.py:57-64,195-204`（unified diff 已返回）
- `agent/tools/file_tools/write_file.py:211`（需改——加 diff）

### ZCode 参考
- `ZCode-main/.../packages/ui/src/components/ui/lightweight-diff-preview.tsx:37-111`（前缀分类 + 行着色）
- `ZCode-main/.../packages/ui/src/lib/patchDiffPreview.ts:296-345`（头剥离逻辑）
- `ZCode-main/.../packages/ui/src/components/ui/highlighted-lightweight-diff-preview.tsx`（Shiki 异步叠加）
- `ZCode-main/.../packages/ui/src/ToolCallBlocks/renderers/EditInlineDiffContent.tsx:38-85`（渲染入口）
