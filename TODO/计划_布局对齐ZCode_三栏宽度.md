# 计划：三栏布局对齐 ZCode

- 建档日期：2026-10-29
- 状态：**待评审**
- **文档范围**：让 sherry 的三栏布局（左侧栏 + 中间对话区 + 右侧栏）对齐 ZCode 的宽度模型。左侧栏最小宽度保持 sherry 原样（280/360px），最大宽度对齐 ZCode（容器 50%）并加可拖拽；中间加 min-width（对齐 ZCode 的 320px）；右侧栏 min/max 对齐 ZCode（240px/65%）；加窄屏自动收起安全网。分两阶段：阶段一 flex 行为对齐（必做），阶段二 Splitter 分数布局完全对齐（可选）。
- **硬约束**：左侧栏最小宽度不变（280px md / 360px lg）；现有 collapsed/maximized/tab 行为不破坏；PrimeVue Splitter 已可用（SessionSidebar 已用）；右侧栏 WIDE_PANEL_KINDS 加宽逻辑保留。
- 关联代码（已核实）：
  - **shell 布局**：
    - `client/app/pages/home/index.vue:3`（根 `flex` 行）、`:20-22`（左侧栏 SessionSidebar）、`:25`（中间 `flex flex-col flex-1 min-w-0` —— **min-w-0 要改**）、`:236-239`（右侧栏 RightSidebar）
  - **左侧栏**：
    - `client/app/pages/home/components/SessionSidebar.vue:6-14`（宽度 `w-[280px] md:w-[280px] lg:w-[360px]` 固定，collapsed=w-0 —— **改为 CSS 变量 + 可拖拽**）
  - **右侧栏宽度管理**：
    - `client/app/stores/right-sidebar.ts:95`（`RIGHT_SIDEBAR_WIDE_PANEL_WIDTH=640`）、`:111-115`（MIN=280/MAX=760/DEFAULT=420 —— **MIN 改 240，cap 改 65%**）、`:127-134`（`clampSidebarWidth` viewportCap 55% —— **改 65%**）、`:276`（`width` 状态 px）、`:305-307`（`setWidth`）、`:314-317`（`fitToViewport`）
    - `client/app/pages/home/components/RightSidebar.vue:13`（transition）、`:16-33`（resize handle `cursor-col-resize` —— **左侧栏复用此模式**）、`:311-312`（`dragging` 状态 + pointer drag 逻辑）
  - **PrimeVue Splitter 已用**：`SessionSidebar.vue` 的 `<Splitter>` + `<SplitterPanel>`（文件树+Git Graph 垂直分割）—— 阶段二水平 Splitter 复用
  - ZCode 参考：
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\WorkspaceShellLayout.tsx:121-141`（常量：264/50%/480/360）、`:2208-2314`（结构：左 flex-none + 内容区 flex-1 min-320 内含 PanelGroup）、`:130-137,629-657`（窄屏自动收起）
    - `D:\selfProj\ZCode\packages\ui\src\app-shell\animatedSidePanePanelModel.ts:57-75`（右侧栏 240px/65%/45%）

---

## 0. 背景（已核实）

### 0.1 sherry 当前布局 vs ZCode 模型

| 维度 | sherry 当前 | ZCode |
|---|---|---|
| **左侧栏宽度** | 固定 280px(md)/360px(lg)，**不可拖拽**，collapsed=w-0 | 可拖拽，264px 起，上限容器 50%，localStorage 持久化 |
| **左侧栏 min** | = 展开宽度（280/360），无独立 min | 264px（= default，不可拖小） |
| **左侧栏 max** | 无（固定值） | 容器 50% |
| **中间 min-width** | `min-w-0`（**可被挤到 0**） | `min-w-[320px]` + 35% |
| **右侧栏位置** | flex 行直接子节点（与中间并列） | 内容区**内部** ResizablePanelGroup 成员 |
| **右侧栏宽度** | 绝对像素 280-760px，可拖拽 | 分数 45% 默认，240px/65% |
| **右侧栏 viewport cap** | 55% | 65% |
| **左扩张时谁让步** | 中间被挤（min-w-0 可到 0），右侧栏不动 | 内容区整体缩小，中间+右侧栏等比例缩（右侧栏到 240px 触底） |
| **窄屏自动收起** | 无 | <480px 收右侧栏，<360px 收左侧栏 |
| **resize handle** | 右侧栏有（`:16-33`） | 左右栏都有 |

**核心问题**：左侧栏扩张时，中间 `min-w-0` 被挤到 0，右侧栏保持固定像素不动——与 ZCode 的"内容区等比例缩小、右侧栏到 240px 触底"不同。

### 0.2 可复用的现有能力

- **PrimeVue Splitter 已在用**：SessionSidebar.vue 的文件树+Git Graph 垂直分割用了 `<Splitter>` + `<SplitterPanel>`。阶段二水平 Splitter 直接可用，无需引入新库。
- **右侧栏拖拽 handle 已有**：RightSidebar.vue:16-33 的 `cursor-col-resize` handle + :311-312 的 pointer drag 逻辑 + right-sidebar store 的 `setWidth`/`clampSidebarWidth`。左侧栏拖拽复用此模式。
- **`fitToViewport`**（right-sidebar.ts:314-317）：窗口缩小时 re-clamp 宽度——窄屏自动收起可在此扩展。

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：左侧栏最小宽度不变 | min 保持 280px(md)/360px(lg)，不可拖到更小 | 拖拽触底 = 当前展开宽度 |
| **F2**：collapsed/maximized 不破坏 | 左侧栏 collapsed=w-0 保留；右侧栏 collapsed/maximized 保留 | 现有折叠/展开行为不变 |
| **F3**：WIDE_PANEL_KINDS 保留 | 宽面板（account/skills 等）打开时加宽逻辑保留 | 宽面板仍加宽 |
| **F4**：中间不被挤到 0 | 加 min-width，左侧栏扩张时中间有下限 | 中间最小 320px |
| **F5**：右侧栏有下限 | 空间不足时右侧栏缩到 240px 触底，不被挤没 | 右侧栏最小 240px |

---

## 2. 阶段一：flex 行为对齐（必做）

### 2.1 目标

左侧栏可拖拽（min 原样/max 50%）+ 中间 min-w-320 + 右侧栏 min-240/max-65% + flex-shrink + 窄屏自动收起。行为对齐 ZCode（都有 min 兜底），不改分数布局。

### 2.2 改动清单

#### (a) 左侧栏：固定宽度 → 可拖拽

- [ ] **2.2.1** 新建 `client/app/stores/sidebar-width.ts`（仿 right-sidebar 的 width 模式）：
  ```typescript
  export const useSidebarWidthStore = defineStore('sidebarWidth', () => {
    /** 展开宽度（px）。默认按断点：md 280 / lg 360。 */
    const width = ref(360);
    /** 折叠态不参与拖拽（w-0），只在展开时可拖。 */
    function setWidth(next: number, viewportWidth?: number): void {
      width.value = clampSidebarWidth(next, viewportWidth);
    }
    return { width, setWidth };
  }, { persist: true });

    /** min 保持 sherry 原样：280px（不可拖到更小）。 */
  export const SIDEBAR_MIN_WIDTH = 280;
  /** max 对齐 ZCode：容器 50%。 */
  export const SIDEBAR_MAX_WIDTH_RATIO = 0.5;

  export function clampSidebarWidth(
    width: number,
    viewportWidth: number = typeof window === 'undefined' ? 0 : window.innerWidth
  ): number {
    const maxCap = viewportWidth > 0 ? Math.floor(viewportWidth * SIDEBAR_MAX_WIDTH_RATIO) : 720;
    return Math.round(Math.min(maxCap, Math.max(SIDEBAR_MIN_WIDTH, width)));
  }
  ```
  - **min = 280px**（F1，保持原样，不可拖小）。ZCode 的 min=264=default，sherry 对齐为 min=280（md 展开宽度）。
  - **max = 容器 50%**（对齐 ZCode `WORKSPACE_SIDEBAR_MAX_WIDTH_RATIO = 0.5`）。
  - localStorage 持久化（对齐 ZCode 的 `zcode:workspace-shell:sidebar-width-px`）。

- [ ] **2.2.2** `SessionSidebar.vue:6-14` 宽度从固定 class 改为 CSS 变量：
  ```vue
  <!-- 原 :11 -->
  'w-[280px] md:w-[280px] lg:w-[360px] border-r ...'
  <!-- 改为 -->
  :style="{ width: collapsed ? '0' : sidebarWidth + 'px' }"
  :class="collapsed ? 'w-0 border-r-0' : 'border-r border-solid ...'"
  ```
  - 内部固定宽度 `:14` 的 `w-[280px] md:w-[280px] lg:w-[360px]` 也改为读 `sidebarWidth`（或保留固定 inner 宽度，外层 overflow-hidden 裁——当前设计就是外层裁内层固定，见 `:13` 注释）。
  - **断点处理**：当前 280(md)/360(lg) 的响应式宽度，改为 store 统一管理。初始化时按 `window.innerWidth` 选 280 或 360 作为默认（lg ≥1024 → 360，否则 280），之后用户拖拽覆盖。

- [ ] **2.2.3** SessionSidebar 右边缘加 resize handle（复用 RightSidebar.vue:16-33 的模式）：
  ```vue
  <!-- 右边缘拖拽手柄（展开时显示） -->
  <div
    v-if="!collapsed"
    class="group absolute right-0 top-0 h-full w-2 z-10 flex items-center justify-center cursor-col-resize"
    @pointerdown="onResizeStart">
    <span class="w-0.5 h-8 rounded" :class="dragging ? 'bg-gray-500' : 'bg-gray-400/70 group-hover:bg-gray-400'" />
  </div>
  ```
  - pointer drag 逻辑复用 RightSidebar.vue:311-312 的 `dragging` + pointermove 计算 `newWidth = startWidth + (startX - currentX)`（左侧栏向左拖增大宽度）→ `sidebarWidthStore.setWidth(newWidth)`。

#### (b) 中间：min-w-0 → min-w-[320px]

- [ ] **2.2.4** `index.vue:25` 的中间容器：
  ```vue
  <!-- 原 -->
  <div class="relative flex flex-col flex-1 min-w-0 h-full ...">
  <!-- 改为 -->
  <div class="relative flex flex-col flex-1 min-w-[320px] h-full ...">
  ```
  - 对齐 ZCode 的 `min-w-[320px]`（`WorkspaceShellLayout.tsx:2309`）。F4：中间不被挤到 0。

#### (c) 右侧栏：min/max 对齐 ZCode + flex-shrink

- [ ] **2.2.5** `right-sidebar.ts:111-115` 宽度常量对齐 ZCode：
  ```typescript
  export const RIGHT_SIDEBAR_MIN_WIDTH = 240;   // 原 280 → 240（对齐 ZCode animatedSidePanePanelModel.ts:57）
  export const RIGHT_SIDEBAR_MAX_WIDTH = 760;    // 保留绝对上限（65% viewport cap 已覆盖）
  export const RIGHT_SIDEBAR_DEFAULT_WIDTH = 420; // 保留
  ```

- [ ] **2.2.6** `right-sidebar.ts:127-134` 的 `clampSidebarWidth` viewportCap 对齐 ZCode：
  ```typescript
  const viewportCap = viewportWidth > 0 ? Math.floor(viewportWidth * 0.65) : RIGHT_SIDEBAR_MAX_WIDTH;
  // 原 0.55 → 0.65（对齐 ZCode maxSize 65%）
  ```

- [ ] **2.2.7** 右侧栏容器加 `flex-shrink`（让空间不足时缩小到 min，而非保持固定像素挤中间）：
  - RightSidebar.vue 的根容器（应用 `:style="{ width: width + 'px' }"` 的元素）加 `shrink min-w-[240px]`：
    ```vue
    :style="{ width: collapsed ? '0' : (maximized ? '100vw' : width + 'px') }"
    :class="collapsed ? 'w-0' : 'shrink min-w-[240px]'"
    ```
  - **行为**：左侧栏扩张 → 中间 flex-1 先缩 → 中间到 320px 触底 → 右侧栏 flex-shrink 开始缩 → 右侧栏到 240px 触底（F5）。这与 ZCode 的"右侧栏到 240px 触底"一致（差异：ZCode 等比例缩，sherry 中间先到 min 再缩右侧栏——阶段二 Splitter 改等比例）。

#### (d) 窄屏自动收起（对齐 ZCode 安全网）

- [ ] **2.2.8** 新建 `client/app/composables/use-auto-collapse.ts`（对齐 ZCode `WorkspaceShellLayout.tsx:629-657`）：
  ```typescript
  const COLLAPSE_SIDE_PANE_PX = 480;   // 对齐 ZCode CONVERSATION_AUTO_COLLAPSE_SIDE_PANE_WIDTH_PX
  const COLLAPSE_SIDEBAR_PX = 360;     // 对齐 ZCode CONVERSATION_AUTO_COLLAPSE_SIDEBAR_WIDTH_PX
  const RESIZE_IDLE_MS = 300;          // 对齐 ZCode CONVERSATION_AUTO_COLLAPSE_RESIZE_IDLE_MS

  export function useAutoCollapse(deps: {
    sidebarCollapsed: Ref<boolean>;
    sidebarWidth: Ref<number>;
    rightSidebarCollapsed: Ref<boolean>;
    rightSidebarWidth: Ref<number>;
  }) {
    let timer: number | undefined;
    function check(): void {
      const viewport = window.innerWidth;
      const contentWidth = viewport - (deps.sidebarCollapsed.value ? 0 : deps.sidebarWidth.value)
        - (deps.rightSidebarCollapsed.value ? 0 : deps.rightSidebarWidth.value);
      // 先收右侧栏
      if (contentWidth < COLLAPSE_SIDE_PANE_PX && !deps.rightSidebarCollapsed.value) {
        deps.rightSidebarCollapsed.value = true;
      }
      // 双 RAF 后再判左侧栏（对齐 ZCode 的两步）
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const content2 = viewport - (deps.sidebarCollapsed.value ? 0 : deps.sidebarWidth.value);
        if (content2 < COLLAPSE_SIDEBAR_PX && !deps.sidebarCollapsed.value) {
          deps.sidebarCollapsed.value = true;
        }
      }));
    }
    function onResize(): void {
      clearTimeout(timer);
      timer = window.setTimeout(check, RESIZE_IDLE_MS);
    }
    onMounted(() => window.addEventListener('resize', onResize));
    onBeforeUnmount(() => window.removeEventListener('resize', onResize));
  }
  ```
  - **仅响应窗口 resize**（对齐 ZCode `:641-642` 注释：手动开面板不触发自动收起）。
  - 优先保中间：先收右侧栏（<480px），再收左侧栏（<360px）。

- [ ] **2.2.9** `index.vue` 的 `onMounted`（:520-532）挂载 `useAutoCollapse`：
  ```typescript
  useAutoCollapse({
    sidebarCollapsed: isSidebarCollapsed,
    sidebarWidth: computed(() => sidebarWidthStore.width),
    rightSidebarCollapsed: computed(() => rightSidebarStore.collapsed),
    rightSidebarWidth: computed(() => rightSidebarStore.width),
  });
  ```

### 2.3 风险与约束

- **flex-shrink 行为 vs ZCode 等比例**：阶段一中间先缩到 320px 再缩右侧栏（flex 加权 shrink），ZCode 是等比例缩。差异在中间到 320px 之前右侧栏不动。阶段二 Splitter 改等比例消除差异。
- **左侧栏断点初始化**：当前 280(md)/360(lg) 响应式，改 store 后需初始化按断点选默认值。首次加载（无 localStorage）按 `window.innerWidth ≥ 1024 ? 360 : 280`，之后用户拖拽覆盖 + 持久化。
- **WIDE_PANEL_KINDS 加宽**（F3）：`openTab` 的 `setWidth(Math.max(width, 640))`（right-sidebar.ts:375,391）逻辑不变——宽面板打开时仍加宽到 640px（若当前更窄）。阶段一右侧栏仍是绝对像素，640px 加宽有效。
- **maximized**（右侧栏填满 viewport）：`toggleMaximized`（:295-298）逻辑不变，maximized 时 width=100vw 覆盖 flex-shrink。

**验收**：F1 + F2 + F3 + F4 + F5。新建 `tests/` 验证 clampSidebarWidth（50% cap / 240px min）+ auto-collapse 阈值。

---

## 3. 阶段二：Splitter 分数布局完全对齐（可选）

### 3.1 目标

中间+右侧栏用 PrimeVue Splitter 水平分数共享（对齐 ZCode 的 ResizablePanelGroup），实现左侧栏扩张时中间+右侧栏**等比例缩小**（而非中间先到 min 再缩右侧栏）。

### 3.2 改动清单

- [ ] **3.2.1** `index.vue:25` 的中间区域内部，把中间对话区 + 右侧栏包进一个水平 `<Splitter>`：
  ```vue
  <div class="relative flex flex-col flex-1 min-w-[320px] h-full ...">
    <!-- 顶部 toolbar 不变 -->
    <Splitter layout="horizontal" class="flex-1 min-h-0">
      <SplitterPanel :size="conversationSize" :minSize="conversationMinSize">
        <NuxtPage ... />   <!-- 中间对话区 -->
      </SplitterPanel>
      <SplitterPanel :size="rightPaneSize" :minSize="rightPaneMinSize">
        <RightSidebar ... />   <!-- 右侧栏 -->
      </SplitterPanel>
    </Splitter>
  </div>
  ```
  - Splitter 已可用（SessionSidebar.vue 已用 `<Splitter>` + `<SplitterPanel>`）。

- [ ] **3.2.2** right-sidebar store 的 width（px）→ 改为 panelSize（%）：
  ```typescript
  // 原: const width = ref(420);  // px
  // 改为: const panelSize = ref(45);  // %（对齐 ZCode SIDE_PANE_DEFAULT_EXPANDED_SIZE = 45%）
  ```
  - `setWidth` → `setPanelSize`（clamp 到 20%-65%，对齐 ZCode min 240px/max 65%）。
  - **WIDE_PANEL_KINDS 加宽**（F3）：`setPanelSize(Math.max(panelSize, 55))`（宽面板打开时给更大比例）。
  - collapsed = panelSize 0（SplitterPanel size=0）。
  - maximized = panelSize 100。

- [ ] **3.2.3** PrimeVue Splitter minSize 是百分比，需运行时把 240px 换算：
  ```typescript
  const rightPaneMinSize = computed(() => {
    const containerWidth = window.innerWidth - sidebarWidthStore.width;
    return Math.max(10, Math.round(240 / containerWidth * 100));  // 240px 下限 → %
  });
  ```
  - 对话区 minSize = 35%（对齐 ZCode `conversation-column minSize="35%"`）。

- [ ] **3.2.4** RightSidebar.vue 的 resize handle（:16-33）移除——Splitter 自带 gutter（拖拽分隔条）。或保留 handle 但改为调 `setPanelSize`。

### 3.3 风险与约束

- **minSize 百分比换算**：PrimeVue Splitter 的 `minSize` 是百分比（非像素），240px 下限需 `240/containerWidth*100` 运行时换算。窗口 resize 时 minSize 需重算（对齐 ZCode 的动态 minSize）。
- **右侧栏绝对像素 → 百分比的迁移**：WIDE_PANEL_KINDS（640px 加宽）、RIGHT_SIDEBAR_PANEL_MIN_WIDTH（420px layout floor）等像素常量需换算为百分比或保留为 panel 内部 min-width（panel 内容仍可用绝对像素 min-width，Splitter 的 size 是面板比例）。
- **collapsed/maximized 与 Splitter 适配**：collapsed = panel size 0（Splitter 支持 size=0）；maximized = 对话区 size 0 + 右侧栏 size 100。需确认 PrimeVue Splitter 支持 size=0 的 panel（SessionSidebar 的垂直 Splitter 已用，可参考）。
- **持久化**：Splitter 的 panel size 通过 right-sidebar store 的 `panelSize` 持久化（对齐 ZCode 的 `useDefaultLayout` layoutId 分数持久化）。

**验收**：阶段一全部 + 左侧栏扩张时中间+右侧栏等比例缩小（非中间先到 min）。

---

## 4. 执行顺序

```
§2 阶段一 flex 行为对齐（2-3 天）
  ├─ sidebar-width store（min 280/max 50%/localStorage）
  ├─ SessionSidebar 宽度改 CSS 变量 + 右边缘 resize handle（复用 RightSidebar 模式）
  ├─ index.vue 中间 min-w-0 → min-w-[320px]
  ├─ right-sidebar MIN 280→240 / viewportCap 55%→65%
  ├─ 右侧栏容器加 shrink min-w-[240px]
  └─ use-auto-collapse（<480 收右侧栏 / <360 收左侧栏）
      │
      ↓ （可选，完全对齐）
§3 阶段二 Splitter 分数布局（2-3 天）
  ├─ 中间+右侧栏包进水平 Splitter
  ├─ right-sidebar width(px) → panelSize(%)
  ├─ minSize 百分比换算（240px → %）
  ├─ WIDE_PANEL_KINDS 加宽改百分比
  └─ collapsed/maximized 适配 Splitter size

总工程量：2-3 天（阶段一）/ 4-6 天（含阶段二）
```

---

## 5. 验收标准

- [ ] **F1**：左侧栏 min 280px（不可拖小），max 容器 50%（对齐 ZCode）。
- [ ] **F2**：左侧栏 collapsed=w-0 保留；右侧栏 collapsed/maximized 保留。
- [ ] **F3**：WIDE_PANEL_KINDS 宽面板打开仍加宽。
- [ ] **F4**：中间 min-w-[320px]，左侧栏扩张时不被挤到 0。
- [ ] **F5**：右侧栏 min 240px，空间不足时缩到 240px 触底。
- [ ] 左侧栏可拖拽 resize，宽度 localStorage 持久化。
- [ ] 右侧栏 viewport cap 65%（对齐 ZCode）。
- [ ] 窄屏：对话区 <480px 自动收右侧栏，<360px 自动收左侧栏。
- [ ] （阶段二）左侧栏扩张时中间+右侧栏等比例缩小（Splitter 分数共享）。

---

## 6. 不做的事

| 项 | 不做的原因 |
|---|---|
| 左侧栏 collapsed 改为窄条保留 | sherry 当前 collapsed=w-0（完全收起），用户未要求改；ZCode 的 collapsed 语义不同 |
| 引入 react-resizable-panels | PrimeVue Splitter 已在用（SessionSidebar），无需引入 React 库的 Vue 等价物 |
| 右侧栏 overlay 模式 | ZCode 有 overlay（panel 浮在对话上），sherry 当前是 inline（占宽度）；未要求改 |
| 左侧栏键盘步进 resize | ZCode 有键盘 16px 步进（`WORKSPACE_SIDEBAR_RESIZE_KEYBOARD_STEP_PX`），MVP 只做 pointer drag |

---

## 7. 关键参考

### sherry 内部
- `client/app/pages/home/index.vue:3,20-22,25,236-239`（三栏 flex 结构）
- `client/app/pages/home/components/SessionSidebar.vue:6-14`（左侧栏宽度 + collapsed）
- `client/app/stores/right-sidebar.ts:95,111-115,127-134,276,305-307,314-317`（右侧栏宽度管理全套）
- `client/app/pages/home/components/RightSidebar.vue:13,16-33,311-312`（resize handle + dragging —— 左侧栏复用模式）
- `client/app/pages/home/components/SessionSidebar.vue`（`<Splitter>` + `<SplitterPanel>` 已用 —— 阶段二复用）

### ZCode 参考
- `D:\selfProj\ZCode\packages\ui\src\app-shell\WorkspaceShellLayout.tsx:121-141`（常量：264/50%/480/360/300ms）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\WorkspaceShellLayout.tsx:2208-2314`（结构：左 flex-none + 内容区 flex-1 min-320 内含 PanelGroup）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\WorkspaceShellLayout.tsx:629-657`（窄屏自动收起：先右侧栏后左侧栏）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\animatedSidePanePanelModel.ts:57-75`（右侧栏 240px/65%/45%）
- `D:\selfProj\ZCode\packages\ui\src\app-shell\sidePaneLayout.ts`（SIDE_PANE_DEFAULT_EXPANDED_SIZE = 45%）
