# 计划：Markdown 画廊与 Lightbox（对齐 ZCode）

- 日期：2026-11-10
- 状态：**待评审**
- 来源：ZCode 对齐 — ZCode 将连续的 `<img>` 节点包裹在画廊容器中，显示缩略图加载 shimmer，并以 `initialIndex`/`items`/键盘焦点恢复打开 lightbox
- 风险等级：**中等** — 纯前端；涉及 markdown 渲染器、预览单例和 DOMPurify 白名单
- 依赖：无（独立的前端特性）

---

## 0. ZCode 行为

ZCode 的聊天 markdown 渲染器：
1. 将连续的 `<img>` 节点包裹在 `data-markdown-image-gallery` 容器中
2. 每张图片获得一个 `data-markdown-image-trigger` / `data-image-thumbnail-trigger` 按钮
3. 缩略图在加载时显示 `markdown-image-loading-shimmer`；失败时显示 `data-markdown-image-state:"error"`
4. 点击缩略图打开**lightbox** 对话框（`dialogTestId:"node-repl-image-lightbox"`），含：
   - `initialIndex` — 画廊中从哪张图片开始
   - `items` — 完整图片列表
   - 键盘导航（上一张/下一张箭头）
   - 关闭时恢复焦点
5. 多张图片（`>=2`）被分组：`data-node-repl-image-gallery`
6. Artifact-URI 图片引用（`zcode-artifact://...`）通过正则 `qTn` 提取并解析

## 1. Sherry 当前状态 — lightbox 已存在，仅缺画廊模型

### 1.1 完整 lightbox 已实现 — `ImagePreviewOverlay.vue`

**`client/app/components/ImagePreviewOverlay.vue`**（217 行）是一个**功能完备的 lightbox**：
- 滚轮缩放（scale 0.2–5，`onWheel` 第 94 行）
- 拖拽平移（`onMouseDown`/`onMouseMove`/`onMouseUp`，第 99-119 行）
- 键盘：ESC 关闭（第 13 行），Tab 陷阱（`trapTab` 第 122-128 行）
- 焦点管理：保存/恢复 `previouslyFocusedEl`（第 130-144 行）
- Teleport 到 body，淡入过渡
- i18n（en/ja/ko/zh），ARIA dialog
- 在 `app.vue:13` 全局挂载

**这不是桩代码 — 是生产级 lightbox。** 本计划无需构建 lightbox，只需将数据模型从单图片升级为多图片画廊。

### 1.2 图片标记模式已存在 — `markFormulaZoom` + `delegatedAct`

**`client/app/directives/safeHtml.ts`** 已有将元素标记为可缩放的精确模式：

- **`markFormulaZoom(el)`**（第 195-206 行）：净化后增强，为 `.katex` 元素添加 `data-act="zoom"`、`role="button"`、`tabindex="0"`、`aria-label`、`.katex-zoom` class
- **`delegatedAct(event)`**（第 246-289 行）：单一委托点击 handler 按 `data-act` 分发 — `zoom` 分支（第 262-288 行）已为 SVG 缩放调用 `useImagePreview().openPreview(dataUrl)`
- **`ALLOW_DATA_ATTR: false`**（`security.ts:83`）意味着 `data-*` 必须在净化后添加（正是 `markFormulaZoom` 所做的）

一个为 `<img>` 元素添加标记的 `markMarkdownImages()` 函数只需几行，完全镜像 `markFormulaZoom`。

### 1.3 DOMPurify 白名单已允许图片

**`client/app/constants/security.ts:30-84`**：`img`（第 62 行）、`figure`（第 64 行）、`figcaption`（第 65 行）及 `src`/`alt`（第 72-73 行）均被允许。Markdown 图片已渲染为 `<img>` — 只是缺少点击接线。

### 1.4 真正缺失的部分

1. **画廊数据模型** — `useImagePreview.ts` 持有一个 `previewSrc` 字符串。需要：`items[]` + `currentIndex` + `next()`/`prev()`
2. **overlay 中的上一张/下一张控件**（箭头 + 键盘 ArrowLeft/ArrowRight + 索引显示）
3. **`markMarkdownImages()`** — 净化后处理，将 `<img>` 标记为可点击（镜像 `markFormulaZoom`）
4. **委托点击 handler 扩展** — 在 `delegatedAct` 中匹配 `img.markdown-image`，收集同一 `[data-message-id]` 气泡中的所有图片，调用 `openGallery(items, startIndex)`

## 2. 设计

### 2.1 将 `useImagePreview` 升级为画廊模型

```typescript
interface GalleryState {
  items: string[]      // image src list
  currentIndex: number
  visible: boolean
}

const gallery = ref<GalleryState>({ items: [], currentIndex: 0, visible: false })

export function openGallery(items: string[], startIndex: number = 0) {
  gallery.value = { items, startIndex, visible: true }
}
export function openPreview(src: string) {  // backward compat
  openGallery([src], 0)
}
export function nextImage() { ... }
export function prevImage() { ... }
export function closePreview() { ... }
```

### 2.2 升级 `ImagePreviewOverlay.vue`

新增：
- 当 `items.length > 1` 时显示上一张/下一张箭头按钮（←/→）
- 键盘：ArrowLeft/ArrowRight 导航（在现有 ESC/滚轮缩放之外）
- `currentIndex` 显示（如 "2 / 5"）
- 滑动支持（可选，用于触控）

### 2.3 使 markdown 图片可点击

两个方案：
- **A. 自定义 `md.renderer.rules.image`** — 在 `safeHtml.ts` 中覆盖以输出包裹器。问题：DOMPurify 的 `ALLOW_DATA_ATTR: false` 会剥离 `data-*`，且 `<button>`/`<div>` 包裹器不在白名单中。
- **B. 净化后增强处理（推荐）** — 在 `safeHtml.ts` 的 `enhance()` 旁新增 `markMarkdownImages()`：
  ```typescript
  function markMarkdownImages(container: HTMLElement) {
    const imgs = container.querySelectorAll('img:not([data-act])')
    imgs.forEach(img => {
      img.classList.add('markdown-image', 'cursor-zoom-in')
      img.setAttribute('role', 'button')
      img.setAttribute('tabindex', '0')
    })
  }
  ```
  然后扩展委托点击 handler 匹配 `img.markdown-image`：
  ```typescript
  if (target instanceof HTMLImageElement && target.classList.contains('markdown-image')) {
    // Collect ALL image srcs in the same message bubble for gallery
    const bubble = target.closest('[data-message-id]')
    const allImgs = bubble?.querySelectorAll('img.markdown-image') ?? []
    const items = Array.from(allImgs).map(i => i.src)
    const startIndex = Array.from(allImgs).indexOf(target)
    openGallery(items, startIndex)
  }
  ```

这镜像现有的 `markFormulaZoom()` 模式（为 `.katex` 添加 `data-act="zoom"`），但使用基于 class 的标记（因为 `ALLOW_DATA_ATTR: false` 会从原始 HTML 剥离 `data-*`）。

### 2.4 加载 shimmer + 错误状态

在增强处理中新增：
- 为每个 `img.markdown-image` 包裹一个加载容器（在 DOM 中创建，而非在 HTML 源码中）
- 当 `img.complete === false` 时显示 shimmer CSS 动画
- 在 `img.onerror` 时添加错误 class + 回退图标

### 2.5 按消息聚合画廊

点击 handler 收集同一 `[data-message-id]` 气泡内的所有 `img.markdown-image`。这将一条 AI 消息中的内联 markdown 图片分组为一个画廊，与 ZCode 的 `data-markdown-image-gallery` 一致。

附件图片（`ChatMediaAttachments.vue`）继续使用 `openPreview(src)`（单图片），除非也升级为 `openGallery()` — 可选增强。

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `client/app/composables/useImagePreview.ts` | 升级为画廊模型：`items[]`、`currentIndex`、`openGallery()`、`nextImage()`、`prevImage()` |
| `client/app/components/ImagePreviewOverlay.vue` | 新增上一张/下一张箭头、键盘导航、索引显示 |
| `client/app/directives/safeHtml.ts` | 新增 `markMarkdownImages()` 增强处理；扩展委托点击 handler 处理 `img.markdown-image` |
| `client/app/components/chat/ChatMediaAttachments.vue` | 升级为 `openGallery()`（可选 — 也聚合附件缩略图） |
| `client/app/assets/css/`（或内联） | 新增 `.markdown-image-loading-shimmer` CSS 动画 |

## 4. 测试

| 文件 | 测试 |
|---|---|
| `client/app/composables/__tests__/useImagePreview.test.ts` | `openGallery([a,b,c], 1)` → currentIndex=1；`nextImage()` 回绕到 0；`prevImage()` 回绕到最后；`closePreview()` 重置 |
| `client/app/directives/__tests__/safeHtml.test.ts` | Markdown `![](url)` → `<img class="markdown-image cursor-zoom-in" role="button" tabindex="0">`；点击图片时以气泡内所有图片调用 `openGallery` |

## 5. 备注

- DOMPurify 白名单（`ALLOW_DATA_ATTR: false`）阻止在 markdown 图片上使用 `data-*` 属性 — 基于 class 的标记是干净路径，与 Mermaid/KaTeX 缩放的做法相同。
- `ImagePreviewOverlay.vue` 已有缩放/平移/ESC/Tab 陷阱/焦点恢复 — 仅新增上一张/下一张导航。
- ZCode 的 `zcode-artifact://` URI 解析由计划 #7（Artifact 持久化）处理 — 本计划仅处理画廊/lightbox UI。
