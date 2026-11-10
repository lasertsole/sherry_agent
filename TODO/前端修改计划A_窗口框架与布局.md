# 前端修改计划 A — 窗口框架与布局适配

> 创建日期：2026-11-09
> 范围：自定义标题栏、面板 padding/滚动、最小宽度/横向滚动、骨架屏

---

## 1. 自定义标题栏（取消系统标题栏）

> 目标：取消操作系统标题栏，自做一个横跨中间栏+右侧栏的自定义标题栏；当右侧栏最大化时，标题栏占满整个窗口宽度。

---

## 一、现状分析

### 当前窗口配置
- `client/src-tauri/tauri.conf.json:13-23`：窗口配置中**无** `decorations`、`titleBarStyle`、`transparent` 字段，使用 OS 原生标题栏。
- `client/src-tauri/capabilities/default.json:6-21`：**无** `core:window:allow-start-dragging`、`allow-minimize`、`allow-maximize`、`allow-close` 等自定义标题栏所需权限。
- 前端**无任何** `data-tauri-drag-region`、titlebar 组件、`-webkit-app-region` 代码。

### 当前布局结构
```
layouts/default.vue  →  .layout (100vw×100vh, flex-col, overflow:hidden)
  └─ <slot> → pages/home/index.vue
       └─ div.flex.flex-row (三栏横向排列)
            ├─ SessionSidebar    (左栏, shrink-0/w-0, 全高, 自带 logo 行 h-15=60px)
            ├─ MiddleColumn      (中间栏, flex-1 或 floating fixed z-50, 顶部工具栏 h-15)
            └─ RightSidebar      (右栏, shrink/w-0 或 maximized=fixed inset-0 z-40)
```

### 关键约束
- **左栏全高**：SessionSidebar 从 y=0 到 y=100vh，自带 logo 行。标题栏不能覆盖左栏。
- **右栏最大化**：`fixed inset-0 z-40`，覆盖整个视口（含左栏和中间栏区域）。此时中间栏弹出为浮窗 `fixed z-50`。
- **浮窗模式**：中间栏变为 `fixed z-50`，脱离 flex 流；浮窗自带 header（h-9=36px）作为拖拽手柄。
- **Web 构建**：非 Tauri 环境无系统标题栏概念（浏览器自带 chrome），标题栏需降级处理。
- **已有 Tauri 检测**：`composables/bridge/transport.ts:15` `isTauri()` → `'__TAURI_INTERNALS__' in window`。

### 目标布局
```
正常状态（左栏展开 + 右栏展开）：
┌──────────┬──────────────────────────────────────┐
│          │  TitleBar (拖拽区 + 窗口控制按钮)     │  ← 仅跨中间+右栏
│  左栏    ├──────────────────────┬───────────────┤
│ (全高)   │                      │               │
│          │  中间栏 (工具栏+聊天) │  右栏         │
│          │                      │               │
└──────────┴──────────────────────┴───────────────┘

右栏最大化时：
┌────────────────────────────────────────────────┐
│  TitleBar (占满整个窗口宽度)                    │  ← 全宽
├────────────────────────────────────────────────┤
│                                                │
│  右栏 (fixed top-36px inset-0, z-40)           │
│  中间栏浮窗 (fixed z-50, 在标题栏下方)          │
│                                                │
└────────────────────────────────────────────────┘
```

---

## 二、修改方案

### 核心思路

TitleBar 作为**单一实例**放在 `layouts/default.vue`，采用 `position: fixed; top: 0; right: 0; z-index: 60`，其 `left` 偏移按路由 + 左栏状态 + 右栏状态动态计算：
- 首页路由 + 左栏展开 + 右栏**未**最大化 → `left = 左栏宽度`（标题栏从左栏右边缘开始）
- 左栏折叠 / 右栏最大化 / 非首页路由 → `left = 0`（标题栏占满全宽）

中间栏+右栏的内容区域通过 `padding-top` 下移，为标题栏腾出空间；左栏保持全高不变。

### z-index 层级规划

| 层级 | z-index | 说明 |
|------|---------|------|
| 右栏最大化 overlay | 40 | `fixed inset-0`（改为 `top: titlebar-h`） |
| 中间栏浮窗 | 50 | `fixed`，浮动窗口 |
| **TitleBar** | **60** | `fixed top-0`，始终在最上层 |
| PrimeVue Dialog/Menu | body teleport | 不受影响（teleport 到 body） |

---

## 三、具体修改清单

### 3.1 Tauri 窗口配置

**文件：`client/src-tauri/tauri.conf.json`**

在 `app.windows[0]` 中追加：
```json
{
  "title": "sherry_agent",
  "decorations": false,
  "transparent": false,
  "width": 800,
  "height": 600,
  "minWidth": 480,
  "minHeight": 640,
  "resizable": true,
  "fullscreen": false
}
```

- `decorations: false`：移除 OS 原生标题栏（Windows/Linux 完全无边框；macOS 需额外处理，见 3.7）。
- `transparent: false`：保持不透明背景，避免 macOS/Linux 透明窗口的合成器开销。

**文件：`client/src-tauri/capabilities/default.json`**

在 `permissions` 数组中追加窗口控制权限：
```json
"core:window:allow-start-dragging",
"core:window:allow-minimize",
"core:window:allow-unminimize",
"core:window:allow-maximize",
"core:window:allow-unmaximize",
"core:window:allow-toggle-maximize",
"core:window:allow-close",
"core:window:allow-is-maximized"
```

### 3.2 新建 TitleBar 组件

**新建文件：`client/app/components/TitleBar.vue`**

```vue
<template>
  <div
    v-if="visible"
    class="titlebar fixed top-0 right-0 flex items-center justify-between z-[60] select-none"
    :style="{ left: leftOffset + 'px', height: TITLEBAR_HEIGHT + 'px' }"
    :class="{ 'titlebar--maximized': rightMaximized }">
    <!-- 左侧：拖拽区（可放应用名/logo） -->
    <div
      class="flex items-center flex-1 h-full px-3"
      data-tauri-drag-region>
      <span
        v-if="!isDesktop"
        class="text-xs text-gray-500 dark:text-gray-400 truncate"
        >{{ appTitle }}</span
      >
    </div>
    <!-- 右侧：窗口控制按钮（仅 Tauri 桌面端） -->
    <div
      v-if="isDesktop"
      class="flex items-center h-full"
      data-tauri-drag-region>
      <button
        v-for="btn in controlButtons"
        :key="btn.action"
        type="button"
        class="titlebar-btn"
        :title="btn.title"
        :aria-label="btn.title"
        :data-test="btn.testId"
        @pointerdown.stop
        @click="btn.handler">
        <i :class="btn.icon" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import { useI18n } from 'vue-i18n';
import { isTauri } from '~/composables/bridge/transport';
import { useUiStore } from '~/stores/ui';
import { useRightSidebarStore } from '~/stores/right-sidebar';
import { useSidebarWidthStore } from '~/stores/sidebar-width';

const TITLEBAR_HEIGHT = 36; // px，对应 h-9

const { t } = useI18n();
const route = useRoute();
const isDesktop = isTauri();

const uiStore = useUiStore();
const rightSidebarStore = useRightSidebarStore();
const sidebarWidthStore = useSidebarWidthStore();

// Web 构建也显示标题栏（纯视觉条，无窗口控制按钮）
const visible = true;

// 右栏是否最大化
const rightMaximized = computed(() => rightSidebarStore.maximized);

// 左栏是否展开（仅首页路由有效）
const isHomeRoute = computed(() => route.path.startsWith('/home'));
const leftSidebarVisible = computed(() => isHomeRoute.value && !uiStore.sidebarCollapsed);

// 标题栏左偏移：左栏展开且右栏未最大化时 = 左栏宽度，否则 = 0（全宽）
const leftOffset = computed(() => {
  if (!leftSidebarVisible.value || rightMaximized.value) return 0;
  return sidebarWidthStore.displayWidth;
});

const appTitle = computed(() => t('chatBox.defaultAiName'));

// 窗口控制按钮
const isMaximized = ref(false);
// ... 通过 Tauri getWindow/getCurrentWindow 查询 maximize 状态
const controlButtons = computed(() => [
  { action: 'minimize', icon: 'pi pi-minus', title: t('titlebar.minimize'), testId: 'titlebar-minimize', handler: handleMinimize },
  { action: 'toggleMaximize', icon: isMaximized.value ? 'pi pi-window-maximize' : 'pi pi-stop', title: t('titlebar.maximize'), testId: 'titlebar-maximize', handler: handleToggleMaximize },
  { action: 'close', icon: 'pi pi-times', title: t('titlebar.close'), testId: 'titlebar-close', handler: handleClose }
]);

async function handleMinimize() { /* invoke('plugin:window|minimize') */ }
async function handleToggleMaximize() { /* invoke('plugin:window|toggle_maximize') */ }
async function handleClose() { /* 触发 close-to-tray 流程或直接 close */ }
</script>

<style lang="scss" scoped>
.titlebar {
  background: var(--titlebar-bg, #ffffff);
  border-bottom: 1px solid var(--gray-light, #eef0f3);
  transition: left 0.3s ease;
  // 暗色模式
 :global(.dark) & {
    background: #181820;
    border-bottom-color: #273444;
  }
}
.titlebar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 100%;
  font-size: 10px;
  color: var(--gray-dark, #6b7280);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: background-color 0.15s;

  &:hover {
    background-color: rgba(0, 0, 0, 0.08);
  }
  // 关闭按钮 hover 变红（Windows 惯例）
  &:last-child:hover {
    background-color: #e81123;
    color: #ffffff;
  }
  :global(.dark) & {
    color: #9ca3af;
    &:hover {
      background-color: rgba(255, 255, 255, 0.08);
    }
  }
}
</style>
```

**关键设计点：**
- `data-tauri-drag-region`：Tauri 拖拽属性，按住标题栏空白区域可拖动窗口。
- 按钮 `@pointerdown.stop`：阻止拖拽事件穿透到按钮，确保点击按钮时不触发窗口拖拽。
- `left` 用 `transition` 平滑过渡，与左栏折叠动画同步。
- 暗色模式背景 `#181820`，与右栏最大化和浮窗的暗色背景一致。
- Web 构建也渲染（`visible = true`），但隐藏窗口控制按钮，作为纯视觉条保持布局一致。

### 3.3 修改 layouts/default.vue

**文件：`client/app/layouts/default.vue`**

```vue
<template>
  <a href="#main-content" class="skip-link">{{ t('a11y.skipToMain') }}</a>
  <main id="main-content" class="layout">
    <TitleBar />
    <slot></slot>
  </main>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import TitleBar from '~/components/TitleBar.vue';

const { t } = useI18n();
</script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.layout {
  @include common.fullViewWindow;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>
```

TitleBar 是 `position: fixed`，不参与 `.layout` 的 flex 流，不影响现有布局。

### 3.4 修改 pages/home/index.vue — 中间+右栏内容下移

**目标**：在根 flex row 中，将中间栏和右栏包进一个容器，给该容器加 `padding-top`，左栏保持全高不变。

**当前结构（简化）：**
```html
<div class="relative w-full h-full flex">
  <SessionSidebar v-model:collapsed="isSidebarCollapsed" ... />
  <div ref="middleColumnRef" class="... flex-1 ..."> ... </div>
  <RightSidebar ... />
</div>
```

**修改后结构：**
```html
<div class="relative w-full h-full flex">
  <SessionSidebar v-model:collapsed="isSidebarCollapsed" ... />

  <!-- NEW: 中间+右栏容器，顶部留出标题栏高度 -->
  <div class="flex flex-col flex-1 min-w-0" :style="{ paddingTop: titlebarHeightPx }">
    <!-- 内容行：中间栏 + 右栏（原有逻辑不变） -->
    <div class="flex flex-row flex-1 min-h-0">
      <div ref="middleColumnRef" class="... flex-1 ..."> ... </div>
      <RightSidebar ... />
    </div>
  </div>
</div>
```

**具体改动点：**

1. **在 `<SessionSidebar>` 和中间栏之间插入一个 wrapper div**（`flex flex-col flex-1 min-w-0`），设 `padding-top`。
2. **将中间栏 `<div ref="middleColumnRef">` 和 `<RightSidebar>` 移入该 wrapper 内的 content row div**。
3. `padding-top` 取自一个 computed：
   ```ts
   const TITLEBAR_HEIGHT = 36;
   const titlebarHeightPx = computed(() => {
     // Web 构建也渲染标题栏，所以始终留出高度
     return TITLEBAR_HEIGHT + 'px';
   });
   ```
4. **中间栏 floating 状态不受影响**：floating 时 `middleColumnRef` 变为 `fixed z-50`，脱离 wrapper 的 flex 流，但 `padding-top` 仍在 wrapper 上，右栏内容正确下移。

**注意事项：**
- `sideExtra` 机制（浮窗模式下中间栏腾出宽度分给两侧）仍正常工作——`SessionSidebar` 的 `extra-width` prop 不变，右栏的 `extra-width` prop 不变，wrapper 只是新增了一层不影响宽度的 flex 容器。
- 中间栏 docked 时的 `flex-1` 和右栏的 `shrink` / `min-w-[280px]` / `fixed inset-0` 行为不变，因为它们仍在同一个 content row flex 中。
- 中间栏的 `middleColumnStyle`（docked: `minWidth: max(320px, 35%)`；floating: fixed 几何）不变，但 floating 时的 top 位置需调整（见 3.6）。

### 3.5 修改 RightSidebar 最大化 overlay

**文件：`client/app/pages/home/components/RightSidebar.vue`**

当前最大化时 `fixed inset-0 z-40`（覆盖整个视口含标题栏区域）。修改为从标题栏下方开始：

**当前（第 5-9 行）：**
```html
store.maximized
  ? 'fixed inset-0 z-40 bg-white dark:bg-[#181820]'
  : ...
```

**修改后：**
```html
store.maximized
  ? 'fixed left-0 right-0 bottom-0 z-40 bg-white dark:bg-[#181820]'
  : ...
```
并追加 inline style：
```ts
:style="store.maximized ? { top: TITLEBAR_HEIGHT + 'px', width: '100%' } : (collapsed ? undefined : { width: `${displayWidth}px` })"
```

这样最大化 overlay 从 `top: 36px` 开始，不覆盖标题栏区域，标题栏（z-60）在其上方全宽可见。

**注意**：`TITLEBAR_HEIGHT` 常量需从共享位置导入（建议放 `common/layout.ts` 或新建 `common/titlebar.ts`）。

### 3.6 修改浮窗几何 — 顶部边界避让标题栏

**文件：`client/app/common/floating-window.ts`**

浮窗的 top 最小值当前是 `FLOATING_MARGIN_PX`（16px）。需加上标题栏高度，使浮窗不与标题栏重叠。

**新增常量：**
```ts
/** 标题栏高度，浮窗的 top 下限需避开它。 */
export const TITLEBAR_HEIGHT_PX = 36;
```

**修改 `clampFloatingPosition`**（该函数负责将浮窗位置限制在 slot 内）：
- top 下限从 `FLOATING_MARGIN_PX` 改为 `TITLEBAR_HEIGHT_PX + FLOATING_MARGIN_PX`。
- 底部、左右不变。

**修改 `resolveFloatingPosition`**（计算各 anchor 的默认位置）：
- `center` / `top-*` anchor 的 top 计算加入 `TITLEBAR_HEIGHT_PX` 偏移。
- `bottom-*` anchor 不变（底部对齐不受标题栏影响）。

**修改 `resolveFloatingSize`**：
- 可用高度从 `slotHeight` 改为 `slotHeight - TITLEBAR_HEIGHT_PX`，确保浮窗不超出标题栏下方的可用空间。

**文件：`client/app/pages/home/index.vue`**
- `viewportSize` / `floatSlot` computed 中，slot 的 top 边界需包含标题栏高度偏移。

### 3.7 macOS 特殊处理

macOS 上 `decorations: false` 会移除整个窗口边框含交通灯（红黄绿按钮）。两种方案：

**方案 A（推荐）：自定义按钮统一所有平台**
- `decorations: false` 适用于所有平台。
- macOS 上 TitleBar 的窗口控制按钮放在**左侧**（遵循 macOS 习惯），Windows/Linux 放**右侧**。
- 通过 `platform` 检测或 CSS `:has()` 调整按钮位置。

**方案 B：macOS 保留交通灯**
- macOS 用 `titleBarStyle: "Overlay"` + `hiddenTitle: true`（保留交通灯，隐藏标题文字）。
- macOS 上 TitleBar 左侧留出 ~78px 空间给交通灯。
- 其他平台用 `decorations: false`。

**建议先用方案 A**（统一自定义按钮），后续如有 macOS 用户体验反馈再切方案 B。

### 3.8 close 按钮与 close-to-tray 流程整合

**文件：`client/app/components/CloseToTrayDialog.vue` + `client/src-tauri/src/commands/window.rs`**

当前 close 流程：用户点 OS 标题栏的关闭按钮 → Rust `close_ack` 命令拦截 → 弹出 CloseToTrayDialog。

自定义标题栏后，close 按钮在 TitleBar 组件内。需要：
1. TitleBar 的 close handler 调用与当前一致的关闭逻辑（触发 `close_ack` / 显示 CloseToTrayDialog）。
2. 可复用 `CloseToTrayDialog.vue` 的 `onCloseRequested` 逻辑，或提取为 composable `useWindowClose`。

### 3.9 i18n 国际化

**文件：`client/app/i18n/locales/zh.json`（及 en/ja/ko 同步）**

新增 `titlebar` 命名空间：
```json
{
  "titlebar": {
    "minimize": "最小化",
    "maximize": "最大化",
    "restore": "还原",
    "close": "关闭"
  }
}
```

### 3.10 共享常量

**新建文件：`client/app/common/titlebar.ts`**
```ts
/** 自定义标题栏高度（px），对应 Tailwind h-9。 */
export const TITLEBAR_HEIGHT = 36;

/** 标题栏高度 CSS 值，用于 inline style padding-top。 */
export const TITLEBAR_HEIGHT_CSS = `${TITLEBAR_HEIGHT}px`;
```

供 `TitleBar.vue`、`RightSidebar.vue`、`floating-window.ts`、`pages/home/index.vue` 统一引用。

---

## 四、影响范围与注意事项

### 4.1 不受影响的部分
| 部分 | 原因 |
|------|------|
| 左栏（SessionSidebar） | 保持全高，`shrink-0`/`w-0` 不变，logo 行 h-15 不变 |
| 中间栏工具栏（h-15） | 仍在中间栏内部顶部，只是整体下移了标题栏高度 |
| 右栏 strip（h-15） | 仍在右栏内部顶部，与中间栏工具栏共享同一水平线 |
| PrimeVue Dialog/Menu | teleport 到 body，z-index 在标题栏之上 |
| 浮窗 header（h-9） | 浮窗自带拖拽手柄不变，仅浮窗 top 下限调整 |
| sideExtra 机制 | wrapper 不影响 flex 宽度分配 |
| Ctrl+B 快捷键 | 左栏 toggle 逻辑不变，标题栏 left 自动跟随 |

### 4.2 需联动修改的部分
| 部分 | 修改内容 |
|------|----------|
| `floating-window.ts` | 浮窗 top 下限 + 可用高度减去标题栏高度 |
| `RightSidebar.vue` | 最大化 overlay 的 top 从 0 改为标题栏高度 |
| `CloseToTrayDialog.vue` | close 按钮入口从 OS 标题栏改为 TitleBar 组件 |
| `pages/home/index.vue` | 中间+右栏包裹在 padding-top 容器中 |
| `layouts/default.vue` | 插入 `<TitleBar />` |
| 浮窗 input-only 折叠态 | 折叠态药丸的 top 位置需避让标题栏 |

### 4.3 Web 构建降级
- Web 环境下 `isTauri() = false`，TitleBar 不渲染窗口控制按钮，但仍作为视觉条存在（`visible = true`）。
- 或者 Web 环境完全不渲染 TitleBar（`visible = isTauri()`），此时 `padding-top` 也应为 0。
- **建议**：Web 环境 `visible = false`，`padding-top` 条件归零，避免浏览器中多一条无意义的空白条。

### 4.4 Windows 11 Snap Layout
- Windows 11 的 snap layout 菜单（悬停最大化按钮触发）依赖原生窗口控件。
- 自定义最大化按钮无法触发 snap layout 菜单，这是已知限制。
- 补偿方案：在最大化按钮上提供自定义的窗口布局选项菜单（后续迭代），或接受此限制。

### 4.5 自动收起安全网（useAutoCollapse）
- `useAutoCollapse` 中的视口宽度阈值不变，但浮窗可用性检查 `FLOATING_MIN_VIEWPORT_WIDTH_PX`（640px）需确认在减去标题栏高度后仍合理。
- 标题栏高度 36px 是固定的，不影响横向宽度阈值。

---

## 五、测试计划

### 5.1 单元测试
- `TitleBar.vue`：`leftOffset` computed 在不同路由/左栏状态/右栏状态下的值。
- `floating-window.ts`：`clampFloatingPosition` 的 top 下限 = `TITLEBAR_HEIGHT_PX + FLOATING_MARGIN_PX`。
- `RightSidebar` store：maximized 状态切换。

### 5.2 集成测试
- `pages/home/index.vue`：中间+右栏 wrapper 的 `padding-top` 正确渲染。
- `RightSidebar.vue`：最大化 overlay 的 `top` 样式正确。
- 布局快照：正常 / 左栏折叠 / 右栏最大化 / 浮窗 / 浮窗+右栏最大化 五种状态。

### 5.3 手动验收
1. 窗口可拖拽（按住标题栏空白区域）。
2. 最小化/最大化/还原/关闭按钮正常工作。
3. 左栏展开 → 标题栏从左栏右边缘开始，跨中间+右栏。
4. 左栏折叠 → 标题栏占满全宽。
5. 右栏最大化 → 标题栏占满全宽，右栏 overlay 从标题栏下方开始。
6. 右栏最大化 + 浮窗 → 浮窗在标题栏下方，不重叠。
7. 浮窗拖拽 → 不超出标题栏下边界。
8. 暗色模式 → 标题栏背景 `#181820`，按钮 hover 正常。
9. Web 构建 → 无标题栏，布局正常（无 padding-top 残留）。
10. Ctrl+B → 左栏折叠/展开，标题栏 left 平滑过渡。

---

## 六、文件清单

| 操作 | 文件 |
|------|------|
| 新建 | `client/app/components/TitleBar.vue` |
| 新建 | `client/app/common/titlebar.ts` |
| 修改 | `client/src-tauri/tauri.conf.json`（加 `decorations: false`） |
| 修改 | `client/src-tauri/capabilities/default.json`（加窗口控制权限） |
| 修改 | `client/app/layouts/default.vue`（插入 `<TitleBar />`） |
| 修改 | `client/app/pages/home/index.vue`（中间+右栏 wrapper + padding-top） |
| 修改 | `client/app/pages/home/components/RightSidebar.vue`（最大化 overlay top） |
| 修改 | `client/app/common/floating-window.ts`（浮窗 top 下限 + 可用高度） |
| 修改 | `client/app/components/CloseToTrayDialog.vue`（close 入口整合） |
| 修改 | `client/app/i18n/locales/{zh,en,ja,ko}.json`（titlebar 命名空间） |
| 新建 | `client/app/components/__tests__/TitleBar.integration.test.ts` |

---

## 七、实施顺序

1. **Tauri 配置**（`tauri.conf.json` + `capabilities/default.json`）→ 确认 `decorations: false` 生效，窗口无边框。
2. **共享常量**（`common/titlebar.ts`）→ 定义 `TITLEBAR_HEIGHT`。
3. **TitleBar 组件**（`TitleBar.vue`）→ 拖拽 + 窗口控制按钮 + `leftOffset` computed。
4. **Layout 插入**（`layouts/default.vue`）→ 确认标题栏渲染。
5. **Home 页 wrapper**（`pages/home/index.vue`）→ 中间+右栏 padding-top。
6. **右栏最大化 overlay**（`RightSidebar.vue`）→ top 偏移。
7. **浮窗几何**（`floating-window.ts`）→ top 下限调整。
8. **Close 流程整合**（`CloseToTrayDialog.vue`）→ close 按钮接入。
9. **i18n** → 四语言同步。
10. **测试** → 单元 + 集成 + 手动验收。
11. **Web 降级验证** → 确认 Web 构建无残留。

---


---

## 10. 修复账户页缺少 padding + 最小宽度，及系统设置 tab 标签栏缺少横向滚动

> 目标：1. 账户页加 padding + 最小宽度 + 横向滚动；2. 系统设置 tab 标签栏在窄宽度下可横向滚动。

### 10.1 问题 A：账户页缺少 padding 和最小宽度

**文件：`client/app/pages/home/components/AccountSettingsPanel.vue:2`**

```html
<div class="flex h-full min-h-0 flex-col gap-3 overflow-auto p-1 text-sm">
```

- `p-1`（4px padding）太小，内容贴边显示，与其他面板的 `p-4`（16px）不一致。
- 无 `min-width`，右栏宽度变小时内容被挤压，`overflow-auto` 只有垂直方向生效（因为 flex-col），水平方向无最小宽度约束 → 横向不出现滚动条。

**对比 ConfigPanel.vue:4**：`<div class="flex flex-col gap-3 h-full min-h-0 p-4">` — 用 `p-4` 且有内层 TabView 控制滚动。

**修改方案：**

```html
<!-- 修改前 -->
<div class="flex h-full min-h-0 flex-col gap-3 overflow-auto p-1 text-sm">

<!-- 修改后 -->
<div class="flex h-full min-h-0 flex-col gap-3 overflow-auto p-4 text-sm min-w-[280px]">
```

- `p-1` → `p-4`：与其他面板一致的 16px padding。
- 新增 `min-w-[280px]`：内容最小宽度 280px（与右栏 `min-w-[280px]` 一致），窄于此时 `overflow-auto` 生效出现横向滚动条。

### 10.2 问题 B：系统设置 tab 标签栏窄宽度下无横向滚动

**文件：`client/app/pages/home/components/ConfigPanel.vue:11-13`**

```html
<TabView
  v-model:activeIndex="activeTab"
  class="flex-1 min-h-0">
```

PrimeVue `TabView` 的 tab 标签栏（`.p-tabview-nav`）默认用 flex 布局排列 tab header，但**没有 `overflow-x: auto`**——tab header 会被 flex 压缩而非溢出滚动。当右栏宽度不足以容纳所有 tab 标题（背景/环境/sherry配置/快捷键/安全过滤/记忆设置/桌面 = 7 个 tab）时，tab 标题被压缩到无法阅读，且右侧的 tab 无法点选。

**修改方案：**

给 TabView 的 tab 导航栏添加 `overflow-x: auto`。PrimeVue 的 TabView 导航栏对应 `.p-tabview-nav-content` / `.p-tabview-nav`，通过 `:deep()` 覆盖：

```scss
/* ConfigPanel.vue <style scoped> 中新增 */
:deep(.p-tabview-nav-content) {
  overflow-x: auto;
  /* 隐藏滚动条但保持可滚动（可选，与右栏 tabs-viewport 的 scrollbar-hide 一致） */
  scrollbar-width: thin;
}

:deep(.p-tabview-nav) {
  /* 不让 tab 收缩：保持每个 tab 的自然宽度，溢出时滚动 */
  flex-shrink: 0;
  flex-wrap: nowrap;
}

:deep(.p-tabview-nav li) {
  /* 每个 tab 保持最小宽度，不压缩到不可读 */
  flex-shrink: 0;
  white-space: nowrap;
}
```

**关键点：**
- `flex-wrap: nowrap` + `flex-shrink: 0`：tab 不换行、不收缩，溢出时由 `overflow-x: auto` 产生横向滚动条。
- `white-space: nowrap`：tab 标题不折行。
- 用 `:deep()` 因为 PrimeVue 的 DOM 在 scoped 组件内，需穿透作用域。

### 10.3 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/pages/home/components/AccountSettingsPanel.vue:2` | `p-1` → `p-4`，新增 `min-w-[280px]` |
| 修改 | `client/app/pages/home/components/ConfigPanel.vue` | `<style scoped>` 新增 `:deep(.p-tabview-nav-content)` 等横向滚动样式 |

### 10.4 测试

**账户页：**
- 打开菜单 → 账户 → 内容四周有 16px padding（与其他面板一致）。
- 右栏宽度拖到极窄（< 280px）→ 出现横向滚动条 → 可滚动查看完整内容。
- 右栏正常宽度 → 无横向滚动条（内容自适应）。

**系统设置 tab：**
- 打开系统设置 → 右栏宽度拖到极窄 → tab 标签栏出现横向滚动条 → 可滚动选到所有 tab。
- 右栏正常宽度 → tab 正常显示（无滚动条，tab 不被压缩）。
- tab 标题不被截断或折行。

---


---

## 11. 修复多面板缺少最小宽度 — 左右分栏在窄右栏下严重挤压

> 目标：技能页面和系统设置-模型配置页面在右栏宽度变小时，左右分栏内容不被挤压到不可用，而是出现横向滚动条。

### 11.1 问题分析

#### 问题 A：技能页面（SkillsPanel）左右分栏

**文件：`client/app/pages/home/components/SkillsPanel.vue:189,206-209`**

```html
<!-- 根容器：overflow-y-auto 但无 min-width，无 overflow-x -->
<div class="flex flex-col h-full min-h-0 overflow-y-auto p-4">
  ...
  <!-- 左右分栏：左列 w-72 shrink-0 (288px 固定)，右列隐式 flex-1 -->
  <div class="flex gap-3" style="min-height: 60vh">
    <div class="w-72 shrink-0 flex flex-col gap-1 pr-1">  <!-- 左列：技能列表 288px 固定 -->
    <!-- 右列：编辑表单（无显式 flex-1，靠 gap + 剩余空间） -->
```

**问题**：
- 根容器 `overflow-y-auto` 只处理垂直滚动，`overflow-x` 默认 `visible`（不裁剪、不滚动）。
- 无 `min-width` 约束——当右栏宽度 < 288px（左列 `w-72 shrink-0` 的固定宽度）时，左列不收缩，内容溢出根容器右侧，但无横向滚动条出现（`overflow-x: visible` 让内容被外层裁剪而非滚动）。
- 右列编辑表单被挤压到极窄，输入框、按钮等不可用。

#### 问题 B：系统设置-模型配置页面（LlmModelManager）展开两栏

**文件：`client/app/pages/home/components/LlmModelManager.vue:39`**

```html
<!-- 展开的模型编辑区：grid 两栏 -->
<div
  v-if="!collapsed"
  class="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">
  <div class="flex flex-col gap-2">  <!-- 左列：模型列表 -->
  <!-- 右列：编辑表单 -->
```

**问题**：
- `md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]` ——左列 `minmax(0,240px)` 的最小值是 0，意味着左列可以收缩到 0px。右栏变窄时左列被压缩到不可读。
- `ConfigPanel.vue:4` 根容器 `p-4` 但无 `min-width` ——窄右栏下整个 ConfigPanel 内容被挤压。
- 无横向滚动回退——grid 两栏都被压缩到不可用状态，但无滚动条。

#### 问题 C：通用根因——所有右栏面板缺少 min-width

右栏面板的根容器普遍只有 `overflow-y-auto` + `p-4`，无 `min-width` 约束。右栏宽度可以缩到 `min-w-[280px]`（`RightSidebar.vue:15`），但面板内容的最小可用宽度往往超过 280px（如 SkillsPanel 的 `w-72`=288px 左列 + 右列编辑表单）。

### 11.2 修改方案

#### 11.2.1 SkillsPanel — 根容器加 min-width + overflow-x

**文件：`client/app/pages/home/components/SkillsPanel.vue:189`**

```html
<!-- 修改前 -->
<div class="flex flex-col h-full min-h-0 overflow-y-auto p-4">

<!-- 修改后 -->
<div class="flex flex-col h-full min-h-0 overflow-auto p-4 min-w-[520px]">
```

- `overflow-y-auto` → `overflow-auto`：同时启用横向滚动（垂直保持 auto，横向也 auto——窄时出现横向滚动条，宽时无滚动条）。
- 新增 `min-w-[520px]`：左列 288px + gap 12px + 右列最小 ~220px = ~520px，窄于此时横向滚动。

#### 11.2.2 LlmModelManager — 左列最小宽度 + 展开区 min-width

**文件：`client/app/pages/home/components/LlmModelManager.vue:39`**

```html
<!-- 修改前 -->
<div
  v-if="!collapsed"
  class="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">

<!-- 修改后 -->
<div
  v-if="!collapsed"
  class="grid gap-4 md:grid-cols-[minmax(140px,240px)_minmax(200px,1fr)] min-w-[480px]">
```

- 左列 `minmax(0,240px)` → `minmax(140px,240px)`：左列最小 140px（模型名+图标可读），不再压缩到 0。
- 右列 `minmax(0,1fr)` → `minmax(200px,1fr)`：右列最小 200px（输入框可用），不再压缩到 0。
- 新增 `min-w-[480px]`：140 + gap 16 + 200 + padding = ~480px，窄于此时面板内容超出右栏宽度。

但 grid 的 `min-width` 只约束 grid 本身——如果外层 ConfigPanel 容器无 `overflow-x`，溢出仍不可滚动。需要配合 ConfigPanel 的修改（见下）。

#### 11.2.3 ConfigPanel — 根容器加 overflow-x + min-width

**文件：`client/app/pages/home/components/ConfigPanel.vue:4`**

```html
<!-- 修改前 -->
<div class="flex flex-col gap-3 h-full min-h-0 p-4">

<!-- 修改后 -->
<div class="flex flex-col gap-3 h-full min-h-0 overflow-auto p-4 min-w-[420px]">
```

- `overflow-auto`：同时启用横向滚动。
- `min-w-[420px]`：模型配置展开两栏需要 ~480px，但 ConfigPanel 还包含其他 tab（背景/环境/快捷键等不需要那么宽），取 420px 作为通用最小宽度——窄于此时横向滚动，宽于时无滚动条。

#### 11.2.4 通用模式——所有右栏面板统一加 min-width

建议对所有在右栏 tab 中渲染的面板根容器统一添加 `min-w-[280px]`（与右栏最小宽度一致）作为基线，对有分栏布局的面板（SkillsPanel、LlmModelManager）使用更大的 `min-width`。

受影响的面板（根容器缺 `min-width` 的）：

| 文件 | 当前根容器 class | 建议 min-width |
|------|-----------------|----------------|
| `SkillsPanel.vue:189` | `overflow-y-auto p-4` | `min-w-[520px]` |
| `ConfigPanel.vue:4` | `p-4`（overflow 在 TabView 内） | `min-w-[420px]` + `overflow-auto` |
| `AccountSettingsPanel.vue:2` | `overflow-auto p-1`（第 10 节已改 `p-4 min-w-[280px]`） | 已修复 |
| `MemoryPanel.vue:4` | `overflow-auto p-4` | `min-w-[280px]`（已有 overflow-auto，补 min-width） |
| `SafetyFilterPanel.vue` | 检查根容器 | `min-w-[280px]` |
| `KnowledgeGraphPanel.vue` | 检查根容器 | `min-w-[280px]` |
| `CronPanel.vue` | 检查根容器 | `min-w-[280px]` |
| `WebSearchPanel.vue` | 检查根容器 | `min-w-[280px]` |

### 11.3 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/pages/home/components/SkillsPanel.vue:189` | `overflow-y-auto` → `overflow-auto`，新增 `min-w-[520px]` |
| 修改 | `client/app/pages/home/components/LlmModelManager.vue:39` | grid 左列 `minmax(0,240px)` → `minmax(140px,240px)`，右列 `minmax(0,1fr)` → `minmax(200px,1fr)`，新增 `min-w-[480px]` |
| 修改 | `client/app/pages/home/components/ConfigPanel.vue:4` | 新增 `overflow-auto` + `min-w-[420px]` |
| 检查/修改 | 其他右栏面板根容器 | 统一补 `min-w-[280px]`（MemoryPanel/SafetyFilterPanel/KnowledgeGraphPanel/CronPanel/WebSearchPanel） |

### 11.4 测试

**技能页面：**
- 菜单 → 技能 → 右栏拖到极窄 → 出现横向滚动条 → 可滚动查看左右两列。
- 左列技能列表保持 288px 固定宽度，右列编辑表单保持可读。
- 右栏正常宽度 → 无横向滚动条。

**模型配置：**
- 系统设置 → 模型配置 → 展开一个模型组 → 右栏拖到极窄 → 左列不压缩到 0，保持最小 140px 可读；右列保持最小 200px 可用。
- 极窄时出现横向滚动条。
- 右栏正常宽度 → 两列正常显示。

**通用：**
- 所有右栏面板窄到 280px 以下 → 出现横向滚动条。
- 右栏正常宽度 → 无多余横向滚动条。

---


---

## 13. 中间栏和右栏内容加载时显示骨架屏

> 目标：中间栏（聊天历史）和右栏面板在内容未加载完成时，用骨架屏（Skeleton）替代当前的空白/转圈 spinner，给用户"正在加载这个区域的内容"的视觉预期。

### 13.1 现状分析

#### 中间栏（聊天历史）

**文件：`client/app/pages/home/index/[sid].vue:1270-1294`**

切换会话时 `doLoadFor` 清空 `chatMessages = []`，然后异步 `stream.syncHistory(sid)` 加载历史。历史返回前 `chatMessages` 为空数组 → ChatBox 显示空状态（无消息），用户看到的是一瞬间空白然后消息突然出现，没有加载预期。

当前无任何 loading 状态——`mySidLoaded` 只是"已触发加载"的标志，不反映"加载完成"。

#### 右栏面板

各面板已有 `loading` ref + `<ProgressSpinner>`（一个 2rem 转圈），但：
- 视觉上只是一个孤立的小转圈，不反映将加载的内容结构。
- 右栏面板挂载（`contentMounted`，`RightSidebar.vue:44`）前是空白。

| 面板 | loading 状态 | 当前 loading UI |
|------|-------------|----------------|
| SkillsPanel | `loading` ref + `<ProgressSpinner>` | 第 192-195 行：居中转圈 |
| ConfigPanel | `loading` ref + `<ProgressSpinner>` | 第 5-9 行：居中转圈 |
| MemoryPanel | `loading` ref + `<ProgressSpinner>` | 第 5-9 行：居中转圈 |
| KnowledgeGraphPanel | `loading` ref + `<ProgressSpinner>` | 类似 |
| SafetyFilterPanel | 检查 | 类似 |
| AccountSettingsPanel | `auth.busy` | 无 loading 占位 |

### 13.2 修改方案

用 PrimeVue 的 `<Skeleton>` 组件替代 `<ProgressSpinner>`，按各面板的内容结构模拟骨架形状。

#### 13.2.1 新建通用骨架组件

**新建文件：`client/app/components/chat/PanelSkeleton.vue`**

```vue
<template>
  <div class="flex flex-col gap-3 p-4">
    <!-- 标题行 -->
    <Skeleton width="40%" height="1.2rem" />
    <!-- 描述行 -->
    <Skeleton width="80%" height="0.8rem" />
    <!-- 卡片列表（3~5 个占位卡片） -->
    <div class="flex flex-col gap-2">
      <Skeleton
        v-for="n in rows"
        :key="n"
        height="3.5rem"
        borderRadius="0.5rem" />
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ rows?: number }>(), { rows: 4 });
</script>
```

`rows` 可按面板调整：技能列表 5 行、记忆条目 4 行、配置表单 3 行。

#### 13.2.2 中间栏（聊天历史）骨架屏

**文件：`client/app/pages/home/index/[sid].vue`**

1. 新增 `isHistoryLoading` ref，在 `doLoadFor` 时设为 `true`，在 `syncHistory` 完成后设为 `false`：

```ts
const isHistoryLoading = ref(false);

const doLoadFor = (sid: string) => {
  chatMessages.value = [];
  isHistoryLoading.value = true;   // ← 新增
  // ...
  void stream.syncHistory(sid).finally(() => {
    isHistoryLoading.value = false;  // ← 新增
  });
  // ...
};
```

2. 模板中 ChatBox 上方/替代位置渲染骨架屏：

```html
<!-- 历史加载中：聊天骨架屏 -->
<div v-if="isHistoryLoading && chatMessages.length === 0" class="flex flex-col gap-4 p-4">
  <!-- 模拟 3 条消息的骨架 -->
  <div class="flex gap-2">
    <Skeleton shape="circle" size="2rem" />
    <div class="flex flex-col gap-1 flex-1">
      <Skeleton width="30%" height="0.7rem" />
      <Skeleton width="60%" height="0.7rem" />
    </div>
  </div>
  <div class="flex gap-2 flex-row-reverse">
    <Skeleton shape="circle" size="2rem" />
    <div class="flex flex-col gap-1 flex-1 items-end">
      <Skeleton width="40%" height="0.7rem" />
      <Skeleton width="50%" height="0.7rem" />
    </div>
  </div>
  <div class="flex gap-2">
    <Skeleton shape="circle" size="2rem" />
    <div class="flex flex-col gap-1 flex-1">
      <Skeleton width="25%" height="0.7rem" />
      <Skeleton width="70%" height="0.7rem" />
      <Skeleton width="55%" height="0.7rem" />
    </div>
  </div>
</div>
<!-- 历史加载完成：正常 ChatBox -->
<ChatBox v-else-if="sessionId" ... />
```

**关键点**：只在 `chatMessages.length === 0` 且 `isHistoryLoading` 时显示骨架——已有消息的会话切换（KeepAlive 恢复）不显示骨架。

#### 13.2.3 右栏面板骨架屏

每个面板将 `<ProgressSpinner>` 替换为 `<PanelSkeleton>` 或内联 Skeleton：

**SkillsPanel.vue:191-195** — 修改前：
```html
<div v-if="loading" class="flex items-center justify-center py-8">
  <ProgressSpinner style="width: 2rem; height: 2rem" />
</div>
```

修改后：
```html
<div v-if="loading" class="flex flex-col gap-3">
  <Skeleton width="40%" height="1.2rem" />
  <div class="flex gap-2 flex-wrap">
    <Skeleton v-for="n in 3" :key="n" width="6rem" height="2rem" borderRadius="0.375rem" />
  </div>
  <div class="flex gap-3">
    <div class="w-72 flex flex-col gap-1">
      <Skeleton v-for="n in 5" :key="n" height="2.5rem" borderRadius="0.5rem" />
    </div>
    <div class="flex-1 flex flex-col gap-1">
      <Skeleton width="60%" height="0.8rem" />
      <Skeleton height="2rem" />
      <Skeleton width="80%" height="0.8rem" />
      <Skeleton height="2rem" />
    </div>
  </div>
</div>
```

**ConfigPanel.vue:5-9** — 修改前：
```html
<div v-if="loading" class="flex items-center justify-center py-8">
  <ProgressSpinner style="width: 2rem; height: 2rem" />
</div>
```

修改后：
```html
<div v-if="loading" class="flex flex-col gap-2 p-2">
  <!-- Tab 标签栏骨架 -->
  <div class="flex gap-2">
    <Skeleton v-for="n in 5" :key="n" width="5rem" height="2.2rem" />
  </div>
  <!-- Tab 内容骨架 -->
  <div class="flex flex-col gap-3">
    <Skeleton width="30%" height="1rem" />
    <Skeleton v-for="n in 3" :key="n" height="3rem" borderRadius="0.5rem" />
  </div>
</div>
```

**MemoryPanel.vue:5-9** — 修改前：
```html
<div v-if="loading && !entries.length" class="flex items-center justify-center py-8">
  <ProgressSpinner style="width: 2rem; height: 2rem" />
</div>
```

修改后：
```html
<div v-if="loading && !entries.length" class="flex flex-col gap-3">
  <!-- target tabs 骨架 -->
  <div class="flex gap-1">
    <Skeleton v-for="n in 3" :key="n" width="4rem" height="1.8rem" />
  </div>
  <!-- 容量条骨架 -->
  <Skeleton width="100%" height="0.4rem" borderRadius="full" />
  <!-- 条目卡片骨架 -->
  <Skeleton v-for="n in 4" :key="n" height="3.5rem" borderRadius="0.5rem" />
</div>
```

#### 13.2.4 PrimeVue Skeleton 注册确认

PrimeVue v4 的 Skeleton 组件名为 `<Skeleton>`（从 `primevue/skeleton` 导入）。Nuxt + PrimeVue 通常已全局注册所有组件，需确认 `Skeleton` 可直接使用（检查 `nuxt.config.ts` 的 PrimeVue 配置是否全局注册了所有组件）。

若未全局注册，在组件内显式导入：
```ts
import Skeleton from 'primevue/skeleton';
```

### 13.3 注意事项

| 事项 | 说明 |
|------|------|
| KeepAlive 恢复 | `[sid].vue` 使用 KeepAlive，已加载的会话切换回来时 `chatMessages` 非空 → 不显示骨架（`chatMessages.length === 0` 条件保证） |
| 首次加载 vs 切换 | 首次加载（无消息）显示骨架；会话切换（清空后异步加载）也显示骨架——两者都需要 |
| 右栏 contentMounted | `RightSidebar.vue:44` 的 `contentMounted` 控制面板挂载/卸载；面板卸载时骨架也不渲染。骨架在各面板内部 |
| 暗色模式 | PrimeVue Skeleton 自动适配暗色模式（`--p-skeleton-*` 色阶） |
| 动画 | PrimeVue Skeleton 默认有 `animation="wave"` 涟漪动画，可按需开启 |
| 骨架行数 | 可按面板 `rows` prop 调整或直接内联 |

### 13.4 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 新建 | `client/app/components/chat/PanelSkeleton.vue`（可选通用骨架组件） |
| 修改 | `client/app/pages/home/index/[sid].vue` | 新增 `isHistoryLoading` + 聊天骨架屏模板 |
| 修改 | `client/app/pages/home/components/SkillsPanel.vue` | `<ProgressSpinner>` → Skeleton 骨架 |
| 修改 | `client/app/pages/home/components/ConfigPanel.vue` | `<ProgressSpinner>` → Skeleton 骨架 |
| 修改 | `client/app/pages/home/components/MemoryPanel.vue` | `<ProgressSpinner>` → Skeleton 骨架 |
| 检查/修改 | `KnowledgeGraphPanel.vue` / `SafetyFilterPanel.vue` / `AccountSettingsPanel.vue` | 同理替换 |

### 13.5 测试

- 打开应用首次加载 → 中间栏显示聊天骨架屏（3 条模拟消息）→ 历史加载完成后骨架消失，消息出现。
- 切换会话 → 中间栏瞬间清空后显示骨架 → 新会话历史加载完成 → 骨架消失。
- 已加载的会话 KeepAlive 恢复 → 不显示骨架（消息非空）。
- 菜单 → 技能 → 面板加载中显示骨架（分类按钮 + 左列卡片 + 右列表单） → 加载完成骨架消失。
- 菜单 → 系统设置 → 加载中显示骨架（tab 标签栏 + tab 内容） → 加载完成。
- 菜单 → 记忆 → 加载中显示骨架（target tabs + 容量条 + 条目卡片） → 加载完成。
- 暗色模式下骨架屏颜色正确（深色底）。
