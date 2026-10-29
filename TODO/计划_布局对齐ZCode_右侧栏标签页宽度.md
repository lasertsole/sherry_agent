# 计划：右侧栏标签页宽度对齐 ZCode

- 建档日期：2026-10-29
- 状态：**待评审**
- **文档范围**：把 sherry 右侧栏标签页（tab strip）的宽度行为对齐 ZCode 的"等宽 + 等比伸缩 + 触底横向滚动"自适应模型。sherry 当前 tab 是 `shrink-0`（不缩窄，直接滚动）+ `truncate`（省略号），ZCode 是 `flex-[1_1_9.75rem]`（等比伸缩 basis 156px）+ `min-w-15`/`max-w-39`（60-156px）+ 渐变 mask 淡出（非省略号）+ 关闭按钮激活态常显/非激活 hover 才显 + ResizeObserver 驱动的溢出判定。**sherry 与 ZCode 同用 Tailwind v4，间距比例一致，可直接复用 ZCode 的 class 名**。
- **硬约束**：tab 的拖拽排序（`@dragstart`）不破坏；RecentClosedTabsMenu 保留；激活态高亮保留；WIDE_PANEL_KINDS 加宽逻辑不受影响（那是面板宽度，不是 tab 宽度）。
- 关联代码（已核实）：
  - **改造对象**：
    - `client/app/pages/home/components/RightSidebar.vue:87-114`（tab strip 容器 + tab button + 标题 span + 关闭按钮 —— **核心改造点**）
    - `client/app/pages/home/components/RightSidebar.vue:92`（`shrink-0` → `flex-[1_1_9.75rem]`）、`:102`（`truncate max-w-24` → 渐变 mask）、`:103-106`（关闭按钮常显 → 激活态常显/非激活 hover）
  - **新增**：
    - `client/app/composables/use-tabs-overflow.ts`（ResizeObserver 驱动的溢出判定 + mask 方向）
  - ZCode 参考（Tailwind v4 同款 class 可直接复用）：
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\SidePaneTabTrigger.tsx:43-45,146`（设计注释 + `flex-[1_1_9.75rem] min-w-15 max-w-39`）
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\SidePaneTabTrigger.tsx:240-247,252`（标题 `shrink-0 whitespace-nowrap` + 渐变 mask）
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\sidePaneLayout.ts:4-6,19-28`（`SIDE_PANE_TAB_MIN_WIDTH_PX=60` / `GAP=4` + `resolveSidePaneTabsOverflow`）
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\AnimatedSidePanePanel.tsx:994-1009,604-690`（滚动视口 + ResizeObserver + mask）

---

## 0. 背景（已核实）

### 0.1 sherry 当前 tab 宽度行为

`RightSidebar.vue:87-114`：
```vue
<div class="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">   <!-- :87 视口 -->
  <button v-for="tab in ..." class="group shrink-0 flex items-center gap-1 h-7 px-2 rounded-md text-xs ...">  <!-- :92 tab，shrink-0 不缩 -->
    <span class="truncate max-w-24">{{ tabLabel(tab) }}</span>   <!-- :102 标题，省略号截断，max 96px -->
    <i class="pi pi-times text-[10px] opacity-60 hover:opacity-100" @click.stop="emit('close-tab', tab)"></i>  <!-- :103-106 关闭按钮常显 -->
  </button>
</div>
```

- tab `shrink-0`：**不收缩**，固定宽度，tab 多时直接横向滚动（不缩窄）。
- 标题 `truncate max-w-24`：省略号截断，max 96px。
- 关闭按钮：每个 tab 常显（`opacity-60`）。
- 溢出：`overflow-x-auto` 直接滚动，无溢出判定/mask。

### 0.2 ZCode tab 宽度自适应模型

`SidePaneTabTrigger.tsx:43-45` 设计注释明确：
> tab 默认按 156px 等宽排列；空间不足时以相同 grow/shrink 参数平均收缩到 60px，之后由外层滚动容器承接溢出，避免标题长度改变每个 tab 的宽度。

| 行为 | class | 像素 |
|---|---|---|
| tab basis / 上限 | `flex-[1_1_9.75rem]` / `max-w-39` | 156px |
| tab 下限 | `min-w-15` | 60px |
| 标题 | `shrink-0 whitespace-nowrap` + 渐变 mask | 不收缩，tab `overflow-hidden` 裁 + mask 淡出 |
| 关闭按钮 | 激活态常显，非激活 `group-hover` 才显 | — |
| 溢出 | ResizeObserver 判定 `N*60 + (N-1)*4 > viewport` → 横向滚动 + mask | — |

**关键差异**：sherry 是"不缩窄直接滚动 + 省略号"，ZCode 是"先等比缩窄到 60px 再滚动 + 渐变 mask"。tab 少时两者都不撑满（sherry shrink-0 左对齐，ZCode max-w 封顶左对齐），差异主要在 tab 多/侧栏窄时。

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：tab 拖拽排序不破坏 | `@dragstart` / `isDraggableTab` 保留 | 拖拽 tab 到 composer 仍生效 |
| **F2**：RecentClosedTabsMenu 保留 | 最近关闭菜单不动 | 下拉菜单功能不变 |
| **F3**：激活态高亮保留 | 当前 tab 的 `bg-theme-main/10` 高亮不变 | 激活 tab 视觉区分 |
| **F4**：tab 少时不撑满 | 空间充足时 tab 不平铺撑满侧栏 | tab 左对齐 + 留白 |
| **F5**：tab 多时等比缩窄 | 空间不足时所有 tab 等比缩到 60px | tab 宽度相等，与标题长短无关 |

---

## 2. tab 宽度：shrink-0 → 等比伸缩

### 2.1 改动

- [ ] **2.1.1** `RightSidebar.vue:92` 的 tab button class 从 `shrink-0` 改为 ZCode 同款等比伸缩：
  ```vue
  <!-- 原 :92 -->
  class="group shrink-0 flex items-center gap-1 h-7 px-2 rounded-md text-xs transition-colors"
  <!-- 改为 -->
  class="group flex-[1_1_9.75rem] min-w-15 max-w-39 h-7 justify-start overflow-hidden whitespace-nowrap rounded-md px-1.5 pr-2 text-xs transition-colors"
  ```
  - `flex-[1_1_9.75rem]`：grow=1 / shrink=1 / basis=156px（对齐 ZCode）。
  - `min-w-15`：60px 下限（F5，等比缩窄触底）。
  - `max-w-39`：156px 上限（F4，tab 少时不撑满，封顶 156px 左对齐留白）。
  - `justify-start overflow-hidden whitespace-nowrap`：左对齐 + 裁剪溢出 + 不换行（标题靠 mask 淡出，见 §3）。
  - `px-1.5 pr-2`：padding 微调（给关闭按钮留位，对齐 ZCode 的 `px-1.5 pr-2`）。
  - **去掉 `shrink-0`**：让 tab 参与等比缩窄。

- [ ] **2.1.2** tab 容器（:87）微调——加 `scrollbar-hide`（溢出滚动时隐藏滚动条，对齐 ZCode 的 `!scrollbar-hide`）：
  ```vue
  <!-- 原 :87 -->
  <div class="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
  <!-- 改为 -->
  <div ref="tabsViewport" class="relative flex min-w-0 flex-1 items-center gap-1 overflow-x-auto scrollbar-hide">
  ```
  - `scrollbar-hide`：需确认 sherry 是否有该工具类（Tailwind v4 无内置，需加 CSS `.scrollbar-hide { scrollbar-width: none } ::-webkit-scrollbar { display: none }`，或用 `[@media](hover:hover){::-webkit-scrollbar{display:none}}`）。若无，在全局 CSS 加一个 `.scrollbar-hide` 工具类。
  - 加 `ref="tabsViewport"` + `relative`：为溢出 mask 定位。

### 2.2 行为对照

| 场景 | 改前（shrink-0） | 改后（等比伸缩） |
|---|---|---|
| tab 少（空间充足） | 左对齐不撑满 | 左对齐不撑满（max-w-39 封顶 156px） |
| tab 多（空间刚好够） | 不缩窄，直接滚动 | 等比缩窄（156px→60px），不滚动 |
| tab 很多（缩到 60px 仍不够） | 直接滚动 | 缩到 60px 触底 → 横向滚动 |
| 侧栏拖窄 | tab 不缩，直接滚动 | tab 等比缩窄到 60px → 滚动 |

**验收**：F4 + F5。

---

## 3. 标题：truncate 省略号 → 渐变 mask 淡出

### 3.1 改动

- [ ] **3.1.1** `RightSidebar.vue:102` 的标题 span 从 `truncate max-w-24` 改为 ZCode 同款渐变 mask：
  ```vue
  <!-- 原 :102 -->
  <span class="truncate max-w-24">{{ tabLabel(tab) }}</span>
  <!-- 改为 -->
  <span
    class="shrink-0 whitespace-nowrap overflow-hidden"
    :style="tabTitleMaskStyle(tab.id === store.activeTabId)">{{ tabLabel(tab) }}</span>
  ```
  - `shrink-0 whitespace-nowrap`：标题不收缩、不换行（对齐 ZCode `SidePaneTabTrigger.tsx:252`）。
  - tab 本体 `overflow-hidden`（§2.1.1 已加）裁剪溢出。
  - 渐变 mask 代替省略号（更优雅，长标题淡出而非 `…`）。

- [ ] **3.1.2** `tabTitleMaskStyle` —— 渐变 mask 的 CSS（复用 ZCode 的 mask-image，对齐 `SidePaneTabTrigger.tsx:240-247`）：
  ```typescript
  /** 标题渐变淡出的 offset：激活态/悬停给关闭按钮留位，否则 0。 */
  const TAB_FADE_OFFSET_REM = 1.25;  // 对齐 ZCode --tab-fade-offset

  function tabTitleMaskStyle(showClose: boolean): Record<string, string> {
    const offset = showClose ? `${TAB_FADE_OFFSET_REM}rem` : '0rem';
    return {
      // 对齐 ZCode: black 到 offset+0.5rem 处全显，之后淡出到透明
      maskImage: `linear-gradient(to right, black calc(100% - ${offset} - 0.5rem), transparent calc(100% - ${offset}))`,
      WebkitMaskImage: `linear-gradient(to right, black calc(100% - ${offset} - 0.5rem), transparent calc(100% - ${offset}))`,
    };
  }
  ```
  - 激活态（`tab.id === store.activeTabId`）：offset=1.25rem，给关闭按钮留淡出空间。
  - 非激活态：offset=0，标题占满 tab 宽度淡出（关闭按钮 hover 才显，不占位）。
  - **hover 联动**（可选进阶）：非激活 tab hover 时 offset 变 1.25rem（关闭按钮显现留位）。用 `group-hover` + CSS 变量实现：tab 上加 `:[--tab-fade-offset:0rem] group-hover:[--tab-fade-offset:1.25rem]`，mask 用 `var(--tab-fade-offset)`。

**验收**：标题长时渐变淡出（非省略号 `…`）。

---

## 4. 关闭按钮：常显 → 激活态常显/非激活 hover

### 4.1 改动

- [ ] **4.1.1** `RightSidebar.vue:103-106` 的关闭按钮从常显改为激活态常显/非激活 hover：
  ```vue
  <!-- 原 :103-106 -->
  <i class="pi pi-times text-[10px] opacity-60 hover:opacity-100" @click.stop="emit('close-tab', tab)"></i>
  <!-- 改为 -->
  <i
    class="pi pi-times text-[10px] shrink-0 transition-opacity"
    :class="tab.id === store.activeTabId
      ? 'opacity-100'
      : 'opacity-0 group-hover:opacity-60'"
    :aria-label="t('rightSidebar.closeTab')"
    @click.stop="emit('close-tab', tab)"></i>
  ```
  - 激活态：`opacity-100`（常显，对齐 ZCode `closeVisible={isActive}`）。
  - 非激活态：`opacity-0 group-hover:opacity-60`（hover 才显，对齐 ZCode 非激活 hover/focus 才显）。
  - `shrink-0`：关闭按钮不收缩（保证可点击宽度）。
  - 这与 §3 的标题 mask offset 联动：激活态关闭按钮常显 → 标题 offset=1.25rem 留位；非激活态关闭按钮隐藏 → 标题 offset=0 占满。

**验收**：激活 tab 的 X 常显，非激活 tab hover 才显 X。

---

## 5. 溢出判定 + 滚动 mask

### 5.1 use-tabs-overflow.ts

- [ ] **5.1.1** 新建 `client/app/composables/use-tabs-overflow.ts`（对齐 ZCode `AnimatedSidePanePanel.tsx:604-690` 的 ResizeObserver + `sidePaneLayout.ts:19-28` 的 `resolveSidePaneTabsOverflow`）：
  ```typescript
  /** 对齐 ZCode sidePaneLayout.ts:4-6 */
  const TAB_MIN_WIDTH_PX = 60;   // min-w-15
  const TAB_GAP_PX = 4;          // gap-1

  export function useTabsOverflow(
    viewportRef: Ref<HTMLElement | null>,
    tabCount: Ref<number>
  ) {
    const isOverflowing = ref(false);
    const showLeftMask = ref(false);
    const showRightMask = ref(false);

    function recompute(): void {
      const viewport = viewportRef.value;
      if (!viewport) return;
      const viewportWidth = viewport.clientWidth;
      // 对齐 ZCode resolveSidePaneTabsOverflow: N*60 + (N-1)*4 > viewportWidth
      const minTotalWidth = tabCount.value * TAB_MIN_WIDTH_PX
        + Math.max(0, tabCount.value - 1) * TAB_GAP_PX;
      isOverflowing.value = minTotalWidth > viewportWidth + 1;  // 1px 容差

      // mask 方向：按 scroll 位置
      if (isOverflowing.value) {
        const { scrollLeft, scrollWidth, clientWidth } = viewport;
        showLeftMask.value = scrollLeft > 1;
        showRightMask.value = scrollLeft < scrollWidth - clientWidth - 1;
      } else {
        showLeftMask.value = false;
        showRightMask.value = false;
      }
    }

    onMounted(() => {
      const viewport = viewportRef.value;
      if (!viewport) return;
      const ro = new ResizeObserver(() => requestAnimationFrame(recompute));
      ro.observe(viewport);
      viewport.addEventListener('scroll', recompute, { passive: true });
      window.addEventListener('resize', recompute);
      onBeforeUnmount(() => {
        ro.disconnect();
        viewport.removeEventListener('scroll', recompute);
        window.removeEventListener('resize', recompute);
      });
    });

    watch(tabCount, () => requestAnimationFrame(recompute));
    return { isOverflowing, showLeftMask, showRightMask };
  }
  ```
  - **溢出判定**：tab 缩到 60px 仍放不下 → `isOverflowing=true` → 视口 `overflow-x-auto` 滚动生效（tab 保持 60px 不再缩）。
  - **mask 方向**：左/右/双侧，按 `scrollLeft` 计算（对齐 ZCode `AnimatedSidePanePanel.tsx:999-1008`）。

### 5.2 mask 渲染

- [ ] **5.2.1** `RightSidebar.vue` 的 tab 视口加渐变 mask 元素：
  ```vue
  <div ref="tabsViewport" class="relative flex min-w-0 flex-1 items-center gap-1 overflow-x-auto scrollbar-hide">
    <!-- 左侧渐变 mask（溢出 + 未滚到最左）-->
    <div v-if="showLeftMask" class="pointer-events-none absolute left-0 top-0 z-10 h-full w-4 bg-gradient-to-r from-[var(--surface-bg)] to-transparent"></div>
    <!-- 右侧渐变 mask -->
    <div v-if="showRightMask" class="pointer-events-none absolute right-0 top-0 z-10 h-full w-4 bg-gradient-to-l from-[var(--surface-bg)] to-transparent"></div>
    <!-- tab 列表 -->
    <button v-for="tab in ..." ...>...</button>
  </div>
  ```
  - mask 颜色用侧栏背景色（`from-[var(--surface-bg)]` 或 `from-white dark:from-[#181820]`，对齐侧栏背景）。
  - `pointer-events-none`：mask 不拦截点击。

### 5.3 风险

- **isOverflowing 与 flex 等比缩窄的配合**：tab 用 `flex-[1_1_9.75rem] min-w-15`，空间不足时自动等比缩到 60px。`isOverflowing` 判定的是"缩到 60px 后是否仍放不下"——此时 flex 的 min-w-15 触底，tab 不再缩，溢出部分由 `overflow-x-auto` 滚动承接。两者自然配合，无需额外协调。
- **scrollbar-hide 工具类**：Tailwind v4 无内置 `scrollbar-hide`，需在全局 CSS 加：
  ```css
  .scrollbar-hide { scrollbar-width: none; -ms-overflow-style: none; }
  .scrollbar-hide::-webkit-scrollbar { display: none; }
  ```

**验收**：tab 缩到 60px 仍放不下 → 横向滚动 + 两侧 mask。

---

## 6. 执行顺序

```
§2 tab 等比伸缩（0.5 天）
  ├─ :92 shrink-0 → flex-[1_1_9.75rem] min-w-15 max-w-39
  ├─ :87 加 scrollbar-hide + ref
  └─ 全局 CSS 加 .scrollbar-hide
      │
      ↓
§3 标题渐变 mask（0.5 天）
  ├─ :102 truncate max-w-24 → shrink-0 whitespace-nowrap + mask-image
  └─ tabTitleMaskStyle（激活态 offset 1.25rem / 非激活 0）
      │
      ↓
§4 关闭按钮可见性（0.5 天）
  └─ :103-106 常显 → 激活态 opacity-100 / 非激活 opacity-0 group-hover
      │
      ↓
§5 溢出判定 + mask（0.5 天）
  ├─ use-tabs-overflow.ts（ResizeObserver + 60px 阈值 + mask 方向）
  └─ 视口加左右渐变 mask 元素

总工程量：1.5-2 天
```

---

## 7. 验收标准

- [ ] **F1**：tab 拖拽排序（`@dragstart` / `isDraggableTab`）不破坏。
- [ ] **F2**：RecentClosedTabsMenu 保留。
- [ ] **F3**：激活态高亮（`bg-theme-main/10`）保留。
- [ ] **F4**：tab 少时不撑满（max-w-39 封顶 156px，左对齐留白）。
- [ ] **F5**：tab 多时等比缩窄到 60px（min-w-15 触底），tab 宽度相等与标题长短无关。
- [ ] 标题长时渐变 mask 淡出（非省略号 `…`）。
- [ ] 激活 tab 的关闭按钮常显，非激活 tab hover 才显。
- [ ] tab 缩到 60px 仍放不下 → 横向滚动 + 两侧渐变 mask。
- [ ] 侧栏拖窄 → tab 等比缩窄（联动 §布局对齐的右侧栏宽度变化）。

---

## 8. 不做的事

| 项 | 不做的原因 |
|---|---|
| tab 概览 Popover（SidePaneTabOverview） | sherry 已有 RecentClosedTabsMenu（最近关闭下拉），覆盖类似需求；ZCode 的可搜索 Popover 是增强，MVP 不做 |
| tab 拖拽重排序（tab 之间拖拽换序） | sherry 当前 tab 拖拽是拖到 composer 做 file mention（`@dragstart`），不是 tab 间换序；ZCode 用 SortableContext 做换序，MVP 不做 |
| `+` 新增按钮 | sherry 的 tab 从工具栏/菜单添加，无 `+` 按钮；不需对齐 ZCode 的 `+` |
| tab 宽度按文本长度自适应 | ZCode 是等宽（与标题长短无关），不做按文本长度变宽 |

---

## 9. 关键参考

### sherry 内部
- `client/app/pages/home/components/RightSidebar.vue:87-114`（tab strip —— 核心改造点）
- `client/app/pages/home/components/RightSidebar.vue:92,102,103-106`（tab button / 标题 span / 关闭按钮）
- `client/app/stores/right-sidebar.ts:403-407`（`tabsInScope` —— tab 列表来源）

### ZCode 参考（Tailwind v4 同款 class 可直接复用）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\SidePaneTabTrigger.tsx:43-45`（设计注释：等宽 + 等比伸缩 + 60px 触底）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\SidePaneTabTrigger.tsx:146`（`flex-[1_1_9.75rem] min-w-15 max-w-39`）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\SidePaneTabTrigger.tsx:240-247,252`（标题 `shrink-0 whitespace-nowrap` + 渐变 mask）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\sidePaneLayout.ts:4-6,19-28`（`SIDE_PANE_TAB_MIN_WIDTH_PX=60` / `GAP=4` + `resolveSidePaneTabsOverflow`）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\AnimatedSidePanePanel.tsx:994-1009,604-690`（滚动视口 + ResizeObserver + mask）
