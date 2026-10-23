# 计划：前端 PDF / Office 文档预览

- 建档日期：2026-10-23
- 状态：**待评审**
- **文档范围**：扩展 sherry 文件查看器（`FileViewerPanel.vue`），支持 PDF（pdf.js）和 docx（mammoth.js）预览，消除非 markdown 文件全纯 `<pre>` 的短板。纯前端 + 轻量后端（二进制文件获取）。
- **硬约束**：现有 markdown 文件渲染不变；二进制获取经安全校验；大文件有上限保护。
- 关联代码：
  - sherry 改造对象：
    - `client/app/pages/home/components/FileViewerPanel.vue:23-32,60,65-92`（渲染 + 格式判定 + load）
    - `client/app/composables/bridge/project-files.ts:83-91`（`fetchProjectFile`——当前返回 text content）
    - `client/app/stores/file-viewer.ts:11,22-72`（缓存——当前 text only）
    - `client/app/directives/safeHtml.ts`（markdown 渲染管线——PDF/docx 不走此管线）
  - ZCode 参考：
    - `ZCode-main/.../packages/ui/src/previewPane*.tsx`（PreviewPane 多格式分发）

---

## 0. 背景（已核实）

### 0.1 sherry 现状

`FileViewerPanel.vue:60` 的格式判定只有 `isMarkdown = pathLabel.endsWith('.md')`——**这是唯一的格式分支**。markdown 走 `v-safe-html`（`:23-27`），**其余一切走 `<pre>{{ content }}</pre>`**（`:28-32`），无高亮、无 PDF/Office 渲染。

`fetchProjectFile`（`project-files.ts:83-91`）调 `GET /project/file` 返回 `{path, content, size}`——**content 是文本**，二进制文件会被截断/破坏。文件查看器缓存（`file-viewer.ts:22-72`）也是 text only。

### 0.2 ZCode 做法

ZCode 的 `PreviewPane.tsx` 按格式分发：code / markdown / image / media / PDF（pdf-viewer）/ Office docx/xlsx/pptx（legacy + modern）/ patch fallback。

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：markdown 不变 | `.md` 文件渲染 byte-identical | 现有行为不变 |
| **F2**：二进制安全获取 | PDF/docx 经二进制 endpoint，不截断 | 二进制完整 |
| **F3**：大文件上限 | 超大文件拒绝/分页 | 超限提示 |
| **F4**：fail-open | 不支持的格式退化纯 `<pre>` | 未知格式不崩 |

---

## 2. 任务一：格式分类器

### 2.1 目标

将 `isMarkdown` 判定扩展为多格式分类。

### 2.2 改动清单

- [ ] **2.2.1** `FileViewerPanel.vue:60` 替换 `isMarkdown` 为 `fileFormat` computed：
  ```typescript
  const fileFormat = computed<'markdown' | 'pdf' | 'docx' | 'image' | 'text'>(() => {
    const ext = pathLabel.split('.').pop()?.toLowerCase()
    if (ext === 'md') return 'markdown'
    if (ext === 'pdf') return 'pdf'
    if (ext === 'docx') return 'docx'
    if (['png','jpg','jpeg','gif','webp','svg'].includes(ext || '')) return 'image'
    return 'text'
  })
  ```

**验收**：F4。

---

## 3. 任务二：二进制文件获取

### 3.1 目标

PDF/docx 需要二进制获取（当前 `GET /project/file` 返回 text）。

### 3.2 改动清单

- [ ] **3.2.1** 后端：`GET /project/file/blob`（或扩展现有 `GET /project/file` 加 `?binary=1`）：
  - 返回 `Content-Type` 正确的 binary response
  - 复用 `resolve_within` 安全校验 + `FILE_BROWSER` 大小上限
  - 或前端直接用 `fetch` 获取 blob（已有 gateway token）
- [ ] **3.2.2** `file-viewer.ts` 缓存增二进制变体：
  - `get/put` 增 `Blob` 类型缓存（`content: string | Blob`）
  - 或二进制不走缓存（每次获取，PDF docx 通常不大）

**验收**：F2 + F3。

---

## 4. 任务三：PDF 渲染器

### 4.1 目标

用 pdf.js 在浏览器渲染 PDF。

### 4.2 改动清单

- [ ] **4.2.1** 安装 `pdfjs-dist`（pdf.js 的 npm 包）
- [ ] **4.2.2** 新建 `client/app/pages/home/components/PdfViewer.vue`：
  - 获取二进制 blob → `pdfjs.getDocument(arrayBuffer)`
  - 逐页渲染到 `<canvas>`（懒加载：可见页才渲染）
  - 分页控制（上一页/下一页/页码）
  - 缩放（fit width / 百分比）
  - 大文件保护（页数上限，超出提示分页）
- [ ] **4.2.3** `FileViewerPanel.vue` 模板增 `<PdfViewer>` 分支

**验收**：F3 + F4。

---

## 5. 任务四：docx 渲染器

### 5.1 目标

用 mammoth.js 将 docx 转 HTML 预览。

### 5.2 改动清单

- [ ] **5.1.1** 安装 `mammoth`（浏览器版 `mammoth.browser.js`）
- [ ] **5.2.2** 新建 `client/app/pages/home/components/DocxViewer.vue`：
  - 获取二进制 arrayBuffer → `mammoth.convertToHtml({arrayBuffer})`
  - 输出 HTML 经 `v-safe-html` 渲染（复用安全管线）
  - 样式：基础文档排版
- [ ] **5.2.3** `FileViewerPanel.vue` 模板增 `<DocxViewer>` 分支

**验收**：F4。

---

## 6. 任务五：图片预览

### 6.1 目标

图片文件直接预览（当前走 `<pre>` 显示乱码）。

### 6.2 改动清单

- [ ] **6.2.1** `FileViewerPanel.vue` 增 image 分支：`<img :src="blobUrl">`（二进制 blob → objectURL）
- [ ] **6.2.2** 复用 `ImagePreviewOverlay` 的拖拽/缩放点击放大

**验收**：F4。

---

## 7. 执行顺序

```
§2 格式分类器（0.5 天）
      │
      ↓
§3 二进制获取（1 天）
  ├─ 后端 GET /project/file/blob
  └─ file-viewer.ts 二进制缓存
      │
      ↓ （可并行）
§4 PDF 渲染器（1.5 天）  §5 docx 渲染器（1 天）  §6 图片预览（0.5 天）

总工程量：3-4 天
```

---

## 8. 验收标准

- [ ] **F1**：`.md` 文件渲染不变。
- [ ] **F2**：PDF/docx 二进制完整获取。
- [ ] **F3**：大文件有上限保护。
- [ ] **F4**：未知格式退化 `<pre>`。
- [ ] PDF 逐页 canvas 渲染 + 分页 + 缩放。
- [ ] docx 转 HTML 经 `v-safe-html` 渲染。
- [ ] 图片直接预览。

---

## 9. 不做的事

| 项 | 不做的原因 |
|---|---|
| xlsx / pptx 预览 | 复杂度高（xlsx 需 SheetJS，pptx 需服务端转换），后续再加 |
| Office 在线编辑 | 只做预览 |
| PDF 文本提取/搜索 | 第一版只做视觉渲染 |

---

## 10. 关键参考

### sherry 内部
- `client/app/pages/home/components/FileViewerPanel.vue:23-32,60,65-92`（渲染 + 判定 + load）
- `client/app/composables/bridge/project-files.ts:83-91`（fetchProjectFile）
- `client/app/stores/file-viewer.ts:11,22-72`（缓存）
- `client/app/directives/safeHtml.ts`（安全管线——docx HTML 经此渲染）

### ZCode 参考
- `ZCode-main/.../packages/ui/src/previewPane*.tsx`（多格式 PreviewPane）
- `ZCode-main/.../packages/ui/src/previewPaneCodeContent.tsx`（代码预览）
