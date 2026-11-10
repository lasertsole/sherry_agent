# 前端修改计划 B — 输入框与面板交互修复

> 创建日期：2026-11-09
> 范围：思考按钮 hover、请求按钮防抖、合并加号、Popover 修复、文件选择器、Git Graph 改名、记忆设置修复与迁移、模型配置字段中文化

---

## 2. 思考模式切换按钮 — 大脑图标未包含在 hover 激活样式内

> 目标：鼠标悬停在思考模式切换按钮上时，大脑图标与文字按钮作为一个整体呈现激活（hover）样式。

### 2.1 问题分析

**文件：`client/app/pages/home/components/ThinkingToggle.vue:5-24`**

当前模板结构：
```html
<div class="flex items-center gap-0.5">
  <!-- 大脑图标：独立 <i> 元素，是 Button 的兄弟节点 -->
  <i class="hidden md:inline brain-icon text-sm text-gray-500 dark:text-gray-400"
     aria-hidden="true"></i>

  <!-- PrimeVue Button：hover 时有 variant="text" 的背景高亮 -->
  <Button
    variant="text"
    size="small"
    :label="triggerLabel"
    icon="pi pi-angle-down"
    icon-pos="right"
    :aria-label="t('thinkingToggle.a11y')"
    @click="toggleMenu" />
</div>
```

**根因**：大脑图标 `<i class="brain-icon">` 是 `<Button>` 的**独立兄弟元素**（两者在同一个 `flex items-center gap-0.5` 容器内），而非 Button 的子元素。PrimeVue `variant="text"` 的 hover 背景只作用于 `<Button>` 自身的 `<button>` 元素，不会延伸到其左侧的兄弟 `<i>`。鼠标悬停在文字按钮区域时，按钮背景高亮但大脑图标颜色不变（保持 `text-gray-500`），视觉上图标"游离"在激活区域之外。

**大脑图标样式**（`client/app/assets/css/main.css:224-233`）：用 CSS mask 实现的 SVG 字形，`background-color: currentColor`，颜色跟随 `text-*` class。

### 2.2 修改方案

**方案 A（推荐）：将大脑图标移入 Button 内部，作为 Button 的自定义内容**

PrimeVue Button 支持通过 default slot 自定义内容。将 `brain-icon` 作为 Button 的左侧子元素，使 hover 背景自然覆盖图标。

**修改后模板：**
```html
<div class="flex items-center gap-0.5">
  <Button
    variant="text"
    size="small"
    :aria-label="t('thinkingToggle.a11y')"
    @click="toggleMenu"
    class="thinking-toggle-btn">
    <!-- 大脑图标作为 Button 的子元素，hover 时背景与文字一同高亮 -->
    <i
      class="hidden md:inline brain-icon text-sm mr-1"
      :class="active ? 'text-theme-main' : 'text-gray-500 dark:text-gray-400'"
      aria-hidden="true"></i>
    <span class="text-sm">{{ triggerLabel }}</span>
    <i class="pi pi-angle-down ml-1 text-xs"></i>
  </Button>
</div>
```

**关键改动：**
1. `<i class="brain-icon">` 从 Button 的兄弟移入 Button 内部（作为子元素）。
2. 图标颜色随 `active` 状态变化（开启时 `text-theme-main`，关闭时 `text-gray-500`），与移动端的 `cycleMobile` 按钮保持一致（第 42 行已有此逻辑）。
3. 手动渲染 label + angle-down 图标，不再用 `:label` / `icon` props（PrimeVue 的 label/icon 渲染顺序与自定义内容混用时不可控）。
4. `mr-1` / `ml-1` 控制间距，替代原 `gap-0.5`（gap 对 Button 内部子元素无效）。

**方案 B（替代）：给外层容器加 group hover**

如果不想改 Button 结构，可以给外层 `<div class="flex items-center gap-0.5">` 加 `group` class，用 `group-hover:` 让大脑图标在悬停时变色：

```html
<div class="group flex items-center gap-0.5">
  <i class="hidden md:inline brain-icon text-sm text-gray-500 group-hover:text-theme-main dark:text-gray-400 dark:group-hover:text-theme-main"
     aria-hidden="true"></i>
  <Button ... />
</div>
```

**缺点**：PrimeVue Button 的 hover 高亮只在其自身范围内，所以图标变色与按钮背景高亮区域仍有视觉缝隙（图标在按钮外面，背景不覆盖图标）。只解决了"颜色不变"但没解决"背景区域不连续"。

**结论**：方案 A 更彻底——图标进入按钮内部，hover 背景和文字色变化同时覆盖图标，视觉上是统一的激活区域。

### 2.3 额外改进：激活态图标着色

当前桌面端大脑图标颜色恒为 `text-gray-500 dark:text-gray-400`（第 9 行），不随思考开启/关闭变化。而移动端 `cycleMobile` 按钮（第 42 行）已有 `active ? 'text-theme-main' : 'text-gray-400'` 的区分。

方案 A 中已包含此改进：桌面端图标也跟随 `active` 状态着色，与移动端保持一致，用户一眼可见思考模式是否开启。

### 2.4 文件清单

| 操作 | 文件 |
|------|------|
| 修改 | `client/app/pages/home/components/ThinkingToggle.vue`（模板结构调整 + 图标着色） |

### 2.5 测试

- 桌面端：hover 思考模式按钮 → 大脑图标与文字一同高亮（背景覆盖 + 颜色变化）。
- 桌面端：思考开启 → 大脑图标显示 `text-theme-main`；关闭 → `text-gray-500`。
- 移动端：cycle 按钮行为不变（已有 `active` 着色）。
- 暗色模式：图标颜色正确跟随。

---


---

## 3. 请求类按钮统一加防抖 + 卸载清理

> 目标：新建会话-选择预设-创建会话按钮加 lodash 防抖并在 onMounted 卸载；前端其他所有涉及请求的按钮同样加防抖和对应卸载。

### 3.1 现状分析

**已有防抖基础设施：**
- `lodash-es` 已在 `package.json` 依赖中（`^4.18.1`）。
- `@types/lodash-es` 已安装。
- 自定义指令 `v-debounce`（`client/app/directives/debounce.ts`）已注册，支持 `v-debounce:click.500="handler"` 语法，**指令自身已在 `unmounted` 钩子中清理 timer 并移除监听器**——使用指令的按钮无需手动卸载。
- 现有使用：`inputBox.vue` 发送/语音/停止按钮、`SkillsPanel.vue:345` selectSkill、`CronPanel.vue:43/57` runJob/removeJob。
- `use-draft-persistence.ts` 用 lodash-es `debounce`，在 `destroy()` 中 `cancel()` 每个 timer。

**问题：大量请求类按钮缺少防抖。** 当前仅有 `:loading` / `:disabled` 状态锁的按钮能防止"请求进行中"的重入，但无法防住"请求极快返回后的快速连点"（loading 状态在 microtask 间有间隙），也缺少组件卸载时的 timer 清理。以下分三组列出全部需处理的按钮。

### 3.2 第一组：完全无保护（最高优先级 — 9 处）

这些按钮触发异步网络请求，但既无 `:loading` 也无 `v-debounce`，连点会直接产生重复请求。

| # | 文件:行号 | 按钮功能 | 请求类型 | 处理方式 |
|---|---|---|---|---|
| 1 | `pages/home/components/SafetyFilterPanel.vue:115` | runTest（测试安全规则） | `testSafetyRules` async | 加 `:loading` + `v-debounce:click.500` |
| 2 | `pages/home/components/SafetyFilterPanel.vue:151` | loadLog（读取安全日志） | `readSafetyLog` async | 加 `:loading` + `v-debounce:click.500` |
| 3 | `pages/home/components/ProjectFileTree.vue:17` | reload（刷新文件树） | `fetchProjectTree` async | 加 `:loading` + `v-debounce:click.500` |
| 4 | `pages/home/components/TerminalPanel.vue:24` | loadInfo（加载终端信息） | `fetchTerminalInfo` async | 加 `:loading` + `v-debounce:click.500` |
| 5 | `pages/home/components/BrowserPanel.vue:92` | go（浏览器导航） | `channel.send` nav | 加 `:loading` + `v-debounce:click.300` |
| 6 | `pages/home/components/WebSearchPanel.vue:21` | choose(null)（切换搜索后端） | `await store.select` async | 加 `:loading` + `v-debounce:click.300` |
| 7 | `pages/home/components/FileTreeNode.vue:67` | onClick（展开文件节点） | `fetchProjectTree` async | 加 `v-debounce:click.200`（节点展开无需 loading） |
| 8 | `pages/home/components/MemoryPanel.vue:175` | handleArchive（归档记忆条目） | `archiveMemoryEntry` async | 加 `:loading` 绑定 busy + `v-debounce:click.500` |
| 9 | `pages/home/components/MemoryPanel.vue:182` | handleRestore（恢复记忆条目） | `restoreMemoryEntry` async | 加 `:loading` 绑定 busy + `v-debounce:click.500` |

### 3.3 第二组：仅有 loading/disabled guard（需补防抖 + 卸载 — 21 处）

这些按钮已有 `:loading` 或 `:disabled` 状态锁，能防住"请求进行中"的重入，但**不是防抖**，且无组件卸载时的 timer 清理。需统一加 `v-debounce` 指令（指令自带 unmounted 清理，免手动 onUnmounted）。

| # | 文件:行号 | 按钮功能 | 现有 guard | 补充 |
|---|---|---|---|---|
| 10 | `NewSessionPresetDialog.vue:57` | confirm（创建会话） | `:loading="creating"` + re-entrancy | 加 `v-debounce:click.500` |
| 11 | `OnboardingDialog.vue:132` | finish（完成引导创建会话） | `:loading="creating"` | 加 `v-debounce:click.500` |
| 12 | `OnboardingDialog.vue:119` | skip（跳过引导） | `:disabled="creating"` | 加 `v-debounce:click.500` |
| 13 | `SessionSidebar.vue:227` | handleBatchDelete（批量删除会话） | `:loading` + `:disabled` | 加 `v-debounce:click.500` |
| 14 | `SafetyFilterPanel.vue:90` | saveRules（保存安全规则） | `:loading="saving"` | 加 `v-debounce:click.500` |
| 15 | `MemoryPanel.vue:154` | saveEdit（保存记忆编辑） | `:loading="busy"` | 加 `v-debounce:click.500` |
| 16 | `MemoryPanel.vue:228` | handleAdd（新增记忆条目） | `:loading="busy"` | 加 `v-debounce:click.500` |
| 17 | `MemorySettingsSection.vue:123` | handleSave（保存记忆设置） | `:loading="saving"` | 加 `v-debounce:click.500` |
| 18 | `MemorySettingsSection.vue:131` | handleConsolidate（记忆合并） | `:loading="consolidating"` | 加 `v-debounce:click.500` |
| 19 | `SkillsPanel.vue:222` | runCurator（运行技能审查） | `:loading` + `:disabled` | 加 `v-debounce:click.500` |
| 20 | `SkillsPanel.vue:375` | togglePin（置顶技能） | `:disabled="togglingActiveIds.has"` | 加 `v-debounce:click.300` |
| 21 | `SkillsPanel.vue:506` | performDelete（删除技能） | `:loading` | 加 `v-debounce:click.500` |
| 22 | `KnowledgeGraphPanel.vue:109` | triggerUpload（上传知识文件） | `:loading="uploading"` | 加 `v-debounce:click.500` |
| 23 | `KnowledgeGraphPanel.vue:123` | loadGraph（加载知识图） | `:loading="loading"` | 加 `v-debounce:click.500` |
| 24 | `ConfigPanel.vue:234/344/419` | handleSave / saveDialogSettings | `:loading="saving"` | 加 `v-debounce:click.500` |
| 25 | `ConfigPanel.vue:129/143` | onCodeEmbedderAction | `:loading="codeEmbedderBusy"` | 加 `v-debounce:click.500` |
| 26 | `ChannelSettingsDialog.vue:240` | handleSave（保存渠道） | `:loading="saving"` | 加 `v-debounce:click.500` |
| 27 | `login/index.vue:44` | submit（登录） | `:loading="auth.busy"` + `:disabled` | 加 `v-debounce:click.500` |
| 28 | `TerminalPanel.vue:97` | run（运行终端命令） | `:loading="running"` | 加 `v-debounce:click.300` |
| 29 | `WebSearchPanel.vue:190/244/252` | saveSearxng/clearTavilyKey/saveTavilyKey | `:loading` + `:disabled` | 加 `v-debounce:click.500` |
| 30 | `CronPanel.vue:276` | handleSaveJob（保存定时任务） | `:loading="saving"` | 加 `v-debounce:click.500` |

**补充（同型，未单列）：** `TokenBudgetPanel.vue:151/158` resetUsage/saveCap、`HeartbeatPanel.vue:32` toggle enabled，均属同类，一并处理。

### 3.4 第三组：已有防抖（无需修改 — 确认完整）

| 文件:行号 | 功能 | 防抖方式 |
|---|---|---|
| `inputBox.vue:93` | 发送消息 | `v-debounce:click.500` |
| `inputBox.vue:74` | 语音录制 | `v-debounce:click.300` |
| `inputBox.vue:88` | 停止 | `v-debounce:click.300` |
| `SkillsPanel.vue:345` | selectSkill | `v-debounce:click.300` |
| `CronPanel.vue:43` | runJob | `v-debounce:click.500` |
| `CronPanel.vue:57` | removeJob | `v-debounce:click.500` |
| `use-draft-persistence.ts` | 草稿写入 | lodash-es `debounce` + `destroy()` cancel |

### 3.5 实施方案

**统一用 `v-debounce` 指令，不手动 onUnmounted。**

`v-debounce` 指令（`directives/debounce.ts`）的 `unmounted` 钩子已自动 `clearTimeout(state.timer)` + `removeEventListener`，所以使用该指令的按钮**无需在 `onMounted`/`onUnmounted` 中手动卸载**——指令在元素卸载时自动清理。

**示例（NewSessionPresetDialog.vue）：**

修改前：
```html
<Button
  :label="t('personaPreset.create')"
  icon="pi pi-check"
  :loading="creating"
  :disabled="!selectedId"
  data-test="new-session-confirm"
  @click="confirm" />
```

修改后：
```html
<Button
  :label="t('personaPreset.create')"
  icon="pi pi-check"
  :loading="creating"
  :disabled="!selectedId"
  data-test="new-session-confirm"
  v-debounce:click.500="confirm" />
```

**关键点：**
1. `@click="confirm"` 改为 `v-debounce:click.500="confirm"`（移除 `@click`，由指令接管事件监听）。
2. `:loading="creating"` 保留——loading 状态锁与防抖是互补的：防抖挡住快速连点的第一波，loading 挡住请求进行中的重入。
3. 指令内部通过 Symbol key 存储状态，`unmounted` 时自动清理 timer 和 listener，**无需 `onUnmounted` 手动 cancel**。

**方案选择：v-debounce 指令 vs lodash debounce + onUnmounted**

| 维度 | v-debounce 指令 | lodash debounce + onUnmounted |
|------|----------------|-------------------------------|
| 模板侵入性 | 低（改一行指令） | 高（需 ref + onUnmounted + handler 改造） |
| 卸载清理 | 自动（unmounted 钩子） | 手动（onUnmounted + .cancel()） |
| 现有一致性 | 已有 6 处使用 | 仅 draft-persistence 用 |
| trailing 调用 | 否（leading-edge 后延迟） | 可配 leading/trailing |
| 适用场景 | 按钮点击防抖 | 高频连续事件（输入、resize） |

**结论**：按钮点击场景统一用 `v-debounce` 指令，保持与现有 6 处一致，免手动卸载。仅对需要 trailing edge 或更复杂控制的场景（如草稿持久化）保留 lodash debounce。

### 3.6 防抖延迟选择

| 按钮类型 | 延迟 | 理由 |
|----------|------|------|
| 创建会话 / 保存配置 / 删除 | 500ms | 高危操作，防误连点 |
| 导航 / 搜索 / 终端 | 300ms | 中频操作 |
| 文件树展开 | 200ms | 低风险高频操作 |

### 3.7 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `NewSessionPresetDialog.vue` | confirm 加 `v-debounce:click.500` |
| 修改 | `OnboardingDialog.vue` | finish/skip 加 `v-debounce:click.500` |
| 修改 | `SafetyFilterPanel.vue` | runTest/loadLog/saveRules 加防抖 + loading |
| 修改 | `ProjectFileTree.vue` | reload 加防抖 + loading |
| 修改 | `TerminalPanel.vue` | loadInfo/run 加防抖 + loading |
| 修改 | `BrowserPanel.vue` | go 加防抖 + loading |
| 修改 | `WebSearchPanel.vue` | choose/saveSearxng/clearTavilyKey/saveTavilyKey 加防抖 |
| 修改 | `FileTreeNode.vue` | onClick 加防抖 |
| 修改 | `MemoryPanel.vue` | handleArchive/handleRestore/saveEdit/handleAdd 加防抖 + loading |
| 修改 | `MemorySettingsSection.vue` | handleSave/handleConsolidate 加防抖 |
| 修改 | `SessionSidebar.vue` | handleBatchDelete 加防抖 |
| 修改 | `SkillsPanel.vue` | runCurator/togglePin/performDelete 加防抖 |
| 修改 | `KnowledgeGraphPanel.vue` | triggerUpload/loadGraph 加防抖 |
| 修改 | `ConfigPanel.vue` | handleSave/saveDialogSettings/onCodeEmbedderAction 加防抖 |
| 修改 | `ChannelSettingsDialog.vue` | handleSave 加防抖 |
| 修改 | `login/index.vue` | submit 加防抖 |
| 修改 | `CronPanel.vue` | handleSaveJob 加防抖 |
| 修改 | `TokenBudgetPanel.vue` | resetUsage/saveCap 加防抖 |
| 修改 | `HeartbeatPanel.vue` | toggle enabled 加防抖 |

共 **19 个文件、约 30 处按钮**。

### 3.8 测试

- `NewSessionPresetDialog`：快速连点 confirm → 只触发一次 `createSessionFromPreset`；loading 期间按钮 disabled。
- `SafetyFilterPanel` runTest：连点 → 单次请求；loading 期间禁用。
- `FileTreeNode` onClick：快速展开/折叠 → 不重复 fetch。
- `MemoryPanel` handleArchive/handleRestore：连点 → 单次 archive/restore。
- 组件卸载（如关闭右栏 tab）：pending timer 不残留（指令 unmounted 自动清理）。
- 现有 `v-debounce` 使用处（inputBox/SkillsPanel/CronPanel）行为不变。

---


---

## 4. 合并两个加号按钮 — 移除输入框工具栏上的 MediaMenu 加号，功能并入输入框内的加号菜单

> 目标：去掉输入框上方工具栏的 MediaMenu 加号按钮，将其图片/音频/视频上传功能合并到输入框内（`inputBox.vue`）已有的加号 Popover 菜单中，统一为一个入口。

### 4.1 现状分析

当前存在**两个独立的加号按钮**，均在输入区域附近，功能互斥但视觉上容易混淆：

**加号 A — 输入框工具栏的 MediaMenu（`[sid].vue:196` → `MediaMenu.vue:6-11`）**

```html
<!-- [sid].vue:191-196 — composer dock 上方的小工具栏行 -->
<div class="@container h-8 px-2 flex items-center gap-3 ...">
  <MediaMenu @select="event => handleOperate('toolBar', event)" />
  ...
</div>
```

```html
<!-- MediaMenu.vue:6-11 -->
<Button variant="text" size="small" icon="pi pi-plus"
  :aria-label="t('mediaMenu.a11y')" @click="toggleMenu" />
<Menu ref="mediaMenu" :model="items" popup />
```

- 图标：`pi pi-plus`
- 菜单项：图片上传 / 音频上传 / 视频上传（来自 `tools` 注册表 `[sid].vue` config）
- 选中后 emit `select` 事件 → `[sid].vue` 的 `handleOperate('toolBar', event)` → `triggerImagePicker()` / `triggerAudioPicker()` / `triggerVideoPicker()`
- 位置：composer dock 上方的 `h-8` 小工具栏行（与 AccessModePicker、TasksButton、SessionModelPicker、ThinkingToggle 同行）

**加号 B — 输入框内的加号（`inputBox.vue:24-61`）**

```html
<!-- inputBox.vue:24-61 — sendActions 区域内 -->
<Button v-if="!disabled" icon="pi pi-plus" class="insert-trigger"
  text rounded severity="secondary"
  :aria-label="t('chatInput.insertTrigger')" @click="toggleInsertMenu" />
<Popover ref="insertMenuRef">
  <div class="insert-menu">
    <button @click="openSkillsFromMenu">  技能  $  </button>
    <button @click="openCommandsFromMenu"> 命令  /  </button>
    <button @click="openFilesFromMenu">    文件  @  </button>
  </div>
</Popover>
```

- 图标：`pi pi-plus`
- 菜单项：技能（`$`）/ 命令（`/`）/ 文件（`@`）三个快捷入口
- 位置：输入框右侧 sendActions 区域（与语音按钮、发送按钮同行）

### 4.2 修改方案

**移除加号 A（MediaMenu），将其上传功能合并到加号 B（inputBox insert-trigger Popover）的菜单中。**

合并后加号 B 的 Popover 菜单变为五项：

```
┌─────────────────────────┐
│ 🖼️  图片上传             │
│ 🎵  音频上传             │
│ 🎬  视频上传             │  ← 新增（从 MediaMenu 合并）
│ ─────────────────────── │  ← 分隔线
│ ⚡  技能        $        │
│ /  命令        /        │
│ 📁  文件        @        │
└─────────────────────────┘
```

### 4.3 具体修改

#### 4.3.1 修改 inputBox.vue — 接收上传事件 + 扩展 Popover 菜单

**新增 props/emits：**

```ts
// inputBox.vue <script setup>
const emit = defineEmits<{
  send: [text: string, images?: ...];
  stop: [];
  // 新增：媒体上传事件（转发给父组件处理）
  uploadImage: [];
  uploadAudio: [];
  uploadVideo: [];
}>();
```

**扩展 Popover 菜单（模板部分）：**

```html
<Popover ref="insertMenuRef">
  <div class="insert-menu">
    <!-- 媒体上传组（从 MediaMenu 合并） -->
    <button type="button" class="insert-item" @click="uploadImageFromMenu">
      <span class="pi pi-image" />
      <span>{{ t('chatInput.uploadImage') }}</span>
    </button>
    <button type="button" class="insert-item" @click="uploadAudioFromMenu">
      <span class="pi pi-volume-up" />
      <span>{{ t('chatInput.uploadAudio') }}</span>
    </button>
    <button type="button" class="insert-item" @click="uploadVideoFromMenu">
      <span class="pi pi-video" />
      <span>{{ t('chatInput.uploadVideo') }}</span>
    </button>
    <!-- 分隔线 -->
    <div class="insert-menu-divider"></div>
    <!-- 原有三项快捷入口 -->
    <button type="button" class="insert-item" @click="openSkillsFromMenu">
      <span class="pi pi-bolt" />
      <span>{{ t('chatInput.skillShortcut') }}</span>
      <code>$</code>
    </button>
    <button type="button" class="insert-item" @click="openCommandsFromMenu">
      <span class="pi pi-slash" />
      <span>{{ t('chatInput.commandShortcut') }}</span>
      <code>/</code>
    </button>
    <button type="button" class="insert-item" @click="openFilesFromMenu">
      <span class="pi pi-folder" />
      <span>{{ t('chatInput.fileShortcut') }}</span>
      <code>@</code>
    </button>
  </div>
</Popover>
```

**新增 handler 函数：**

```ts
function uploadImageFromMenu(): void {
  insertMenuRef.value?.hide?.();
  emit('uploadImage');
}
function uploadAudioFromMenu(): void {
  insertMenuRef.value?.hide?.();
  emit('uploadAudio');
}
function uploadVideoFromMenu(): void {
  insertMenuRef.value?.hide?.();
  emit('uploadVideo');
}
```

**新增分隔线样式（`<style>` 部分）：**

```scss
.insert-menu-divider {
  height: 1px;
  margin: 4px 0;
  background: var(--gray-light, #eef0f3);
}
```

#### 4.3.2 修改 [sid].vue — 移除 MediaMenu，绑定上传事件

**模板修改：**

移除 MediaMenu 组件引用：
```html
<!-- 移除这一行 -->
<!-- <MediaMenu @select="event => handleOperate('toolBar', event)" /> -->
```

ChatInputBox 绑定新 emits：
```html
<ChatInputBox
  v-show="!hitlRequest"
  ref="chatInputBoxRef"
  v-model:draft="draft"
  :sending="isSending"
  :disabled="!!hitlRequest"
  :disabled-text="t('chatInput.waitingApproval')"
  :session-id="sessionId"
  @send="onComposerSend"
  @stop="handleStop"
  @upload-image="triggerImagePicker"
  @upload-audio="triggerAudioPicker"
  @upload-video="triggerVideoPicker" />
```

**script 修改：**

- 移除 `import MediaMenu from '../components/MediaMenu.vue'`。
- `triggerImagePicker` / `triggerAudioPicker` / `triggerVideoPicker` 函数保持不变（原由 `handleOperate('toolBar', event)` 分发，现在直接由 emit handler 调用）。
- 移除 `handleOperate` 中 `toolBar` 相关的 media 事件分发逻辑（如仅 MediaMenu 使用）。

#### 4.3.3 删除 MediaMenu.vue

**删除文件：`client/app/pages/home/components/MediaMenu.vue`**

功能已完全合并到 inputBox.vue，该组件不再被引用。

**同步删除测试文件（如有）：**
- `client/app/pages/home/components/__tests__/MediaMenu.integration.test.ts`（如存在）

#### 4.3.4 检查 config.ts 中的 tools 注册表

**文件：`client/app/pages/home/config.ts`**

`MediaMenu.vue` 从 `tools` 注册表读取菜单项（图片/音频/视频）。合并后 inputBox.vue 不再读 `tools` 注册表（直接硬编码三项上传按钮），`config.ts` 中的 `tools` 数组如果仅被 MediaMenu 使用，可清理；如被其他地方引用则保留。

需确认 `tools` 注册表是否还有其他消费者（如 toolbox 对话框）。

### 4.4 注意事项

| 事项 | 说明 |
|------|------|
| HITL 状态 | 加号 B 有 `v-if="!disabled"`，HITL 期间隐藏（MediaMenu 无此约束，在 `v-show="!hitlRequest"` 的工具栏行内）。合并后上传功能也会在 HITL 期间不可用——这是合理的，HITL 期间不应上传媒体。 |
| sendActions 布局 | 加号 B 在 sendActions 区域（与语音/发送按钮同行），菜单项从 3 项增到 6 项 + 分隔线，Popover 高度增加但已有 `max-h` 滚动不需改。 |
| 无障碍 | 上传按钮需添加 `aria-label`；分隔线用 `role="separator"`。 |
| i18n | `chatInput.uploadImage/uploadAudio/uploadVideo` 需新增四语言翻译；`mediaMenu.a11y` 可删除（MediaMenu 已移除）。 |
| 已有防抖 | 加号 B 当前无 `v-debounce`，按本计划第 3 节应加 `v-debounce:click.300`。 |

### 4.5 i18n 新增

**文件：`inputBox.vue` 的 `<i18n>` 块或 `locales/{zh,en,ja,ko}.json`**

```json
{
  "chatInput": {
    "uploadImage": "图片上传 / 上传图片",
    "uploadAudio": "音频上传 / 上传音频",
    "uploadVideo": "视频上传 / 上传视频"
  }
}
```

四语言同步（zh/en/ja/ko）。

### 4.6 文件清单

| 操作 | 文件 |
|------|------|
| 修改 | `client/app/components/chat/inputBox.vue`（扩展 Popover 菜单 + 新增 emits/handler） |
| 修改 | `client/app/pages/home/index/[sid].vue`（移除 MediaMenu 引用 + 绑定上传 emits） |
| 删除 | `client/app/pages/home/components/MediaMenu.vue` |
| 检查 | `client/app/pages/home/config.ts`（`tools` 注册表是否需清理） |
| 修改 | i18n（新增 `chatInput.uploadImage/uploadAudio/uploadVideo`，删除 `mediaMenu.a11y`） |

### 4.7 测试

- 点击输入框内加号 → Popover 显示 6 项（3 上传 + 分隔线 + 3 快捷入口）。
- 点击"图片上传" → 触发文件选择器（原 `triggerImagePicker` 行为）。
- 点击"音频上传"/"视频上传" → 同理。
- 点击"技能"/"命令"/"文件" → 原有行为不变。
- HITL 期间 → 加号按钮隐藏，上传不可用。
- 确认 MediaMenu.vue 无残留引用（grep `MediaMenu` 全局无结果）。
- `@container` 窄屏 → 工具栏行布局正常（MediaMenu 移除后少一个元素，不影响 flex）。

---


---

## 5. 修复输入框加号 Popover 菜单 — 白色框背景 + 点击无反应

> 目标：修复输入框内加号按钮点开后的列表项右侧白色框背景问题，以及点击"技能"/"命令"后看似无反应的问题。

### 5.1 问题分析

#### 问题 A：列表项右侧白色框

**文件：`client/app/components/chat/inputBox.vue:884-891`**

```scss
:global(.insert-menu .insert-item code) {
  margin-left: auto;
  border-radius: 0.25rem;
  background: var(--p-surface-100, #f3f4f6);   /* ← 白色/浅灰背景 */
  padding: 0 0.3rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.72rem;
}
```

每个菜单项右侧的 `<code>$</code>` / `<code>/</code>` / `<code>@</code>` 标签有一个 `background: var(--p-surface-100, #f3f4f6)` 的浅灰/白色背景块。这是**有意为之**的"快捷键标签"样式（类似键盘按键），但在暗色模式下 `--p-surface-100` 仍是浅灰色，与暗色菜单背景对比突兀，看起来像一个"白色框"。

**根因**：`<code>` 的 `background` 用了 PrimeVue 的 `--p-surface-100`（亮色模式下 `#f3f4f6`，暗色模式下 PrimeVue 的 surface 色阶本应变深，但实际渲染中该 CSS 变量在 Popover body 内可能未被暗色主题覆盖，保持浅灰），导致暗色模式下标签背景偏白。

#### 问题 B：点击"技能"/"命令"后看似无反应

**文件：`client/app/components/chat/inputBox.vue:432-440` + `InputTriggerPanel.vue:91-99,148-158`**

点击"技能"的执行链：
```
openSkillsFromMenu()
  → insertMenuRef.value?.hide?.()     // 关闭 Popover
  → openTrigger('skill')               // 设置 triggerKind='skill', query='', anchor=...
  → triggerKind.value = 'skill'        // 触发 v-if="triggerKind" 渲染 InputTriggerPanel
```

`InputTriggerPanel` 渲染后：
```ts
// InputTriggerPanel.vue:91-99
const props = withDefaults(defineProps<{
  kind: TriggerKind;
  query: string;                        // ← 空字符串 ''
  anchor: { left, top, width } | null;
  sessionId?: string;
}>(), { sessionId: '' });

// InputTriggerPanel.vue:148-158 — 面板定位
const panelStyle = computed(() => {
  if (!props.anchor) return {};
  const width = Math.min(420, window.innerWidth - 16);
  const left = Math.min(Math.max(8, props.anchor.left), window.innerWidth - width - 8);
  const bottom = Math.max(8, window.innerHeight - props.anchor.top + 8);
  return { left: `${left}px`, bottom: `${bottom}px`, width: `${width}px` };
});
```

面板通过 `Teleport to="body"` 渲染为 `position: fixed`，`bottom` 定位在输入框顶部上方 8px。**面板确实会渲染**，但有两个可能导致"看着没用"的原因：

1. **面板定位在输入框上方**（`bottom: window.innerHeight - anchor.top + 8`），如果输入框靠近屏幕顶部（如输入框很高或窗口很窄），面板可能被挤出可视区域或被其他元素遮挡。

2. **更可能的原因**：`openTrigger('skill')` 传了 `query=''`，`InputTriggerPanel` 的 `rows` computed 对 skills 做 `scoreTriggerMatch(skill.name, skill.description, '')` 过滤——空 query 的匹配逻辑如果返回 `score < 0`，则 `rows` 为空，面板只显示"暂无技能"（`trigger-empty`），用户看到的是一个空面板或瞬间消失的 Popover（hide + 空面板），以为"没反应"。

3. **Popover hide 与 InputTriggerPanel 显示的时序**：`insertMenuRef.value?.hide?.()` 关闭 Popover 后 `openTrigger('skill')` 立即设置 `triggerKind`，但 Popover 的 hide 动画（~150ms）与 InputTriggerPanel 的 Teleport mount 可能竞争——Popover 关闭时的 `pointer-events` 遮罩可能短暂拦截 InputTriggerPanel 的点击。

**"文件"项正常**：`openFilesFromMenu()` 只设 store 值（`ui.sidebarBody = 'files'`），不涉及 InputTriggerPanel，所以左栏文件树会立即展开，用户能看到响应。

### 5.2 修改方案

#### 5.2.1 修复白色框（`<code>` 标签背景）

**文件：`client/app/components/chat/inputBox.vue:884-891`**

将 `<code>` 的背景改为跟随当前颜色的半透明色，而非固定 surface 色：

```scss
:global(.insert-menu .insert-item code) {
  margin-left: auto;
  border-radius: 0.25rem;
  /* 改：用 currentColor 半透明替代固定 surface 色，暗色模式自适应 */
  background: color-mix(in srgb, currentColor 12%, transparent);
  padding: 0 0.3rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.72rem;
  opacity: 0.7;  /* 让快捷键标签视觉退后，不抢菜单项主文字 */
}
```

`currentColor` 跟随 `.insert-item` 的文字色（亮色 `#374151`，暗色 `#d1d5db`），`color-mix` 12% 透明度让标签有微弱底色但不突兀。去掉 `var(--p-surface-100)` 依赖。

#### 5.2.2 修复点击"技能"/"命令"无反应

**根因是空 query 过滤 + 面板定位/时序问题。** 修复分三步：

**步骤 1：确保空 query 时 `scoreTriggerMatch` 返回非负值（显示全部）**

**文件：`client/app/composables/use-input-trigger.ts`（`scoreTriggerMatch` 函数）**

需确认 `scoreTriggerMatch(name, description, '')` 在空 query 时的行为。如果空 query 返回 `score < 0`（不匹配），则 `rows` 为空。

修复：空 query 时 `scoreTriggerMatch` 应返回 `0`（匹配全部，无排序偏好），让面板显示完整列表。

```ts
// use-input-trigger.ts — scoreTriggerMatch
export function scoreTriggerMatch(name: string, description: string, query: string): number {
  if (!query) return 0;  // ← 新增：空 query 匹配全部
  // ... 原有逻辑
}
```

**步骤 2：确保 InputTriggerPanel 在空 query 时正确渲染并定位**

**文件：`client/app/components/chat/InputTriggerPanel.vue:148-158`**

`panelStyle` 的 `bottom` 计算在输入框靠近顶部时可能产生负值或极小值。添加最小底部偏移：

```ts
const panelStyle = computed(() => {
  if (!props.anchor) return {};
  const width = Math.min(420, window.innerWidth - 16);
  const left = Math.min(Math.max(8, props.anchor.left), window.innerWidth - width - 8);
  const bottom = Math.max(8, window.innerHeight - props.anchor.top + 8);
  return {
    left: `${left}px`,
    bottom: `${bottom}px`,
    width: `${width}px`
  };
});
```

当前 `bottom` 已有 `Math.max(8, ...)` 保护，但如果 `anchor.top` 很小（输入框在屏幕上方），`window.innerHeight - anchor.top + 8` 会很大，面板会定位在很高的位置——这不会导致"看不见"（面板在可视区内），但可能离输入框很远。这不是主要问题。

**步骤 3（关键）：修复 Popover hide 与 InputTriggerPanel 的时序竞争**

**文件：`client/app/components/chat/inputBox.vue:432-440`**

当前代码：
```ts
function openSkillsFromMenu(): void {
  insertMenuRef.value?.hide?.();  // 关闭 Popover（可能触发遮罩/动画）
  openTrigger('skill');           // 立即设置 triggerKind → 渲染 InputTriggerPanel
}
```

Popover 的 `hide()` 是同步调用但 Popover 内部可能有 dismiss 延迟。`openTrigger` 紧随其后设置 `triggerKind`，InputTriggerPanel 通过 Teleport 立即 mount。两者的 z-index 可能冲突（Popover 关闭动画期间 z-index 仍有效）。

修复：用 `nextTick` 确保 Popover 先关闭再打开 trigger panel：

```ts
import { nextTick } from 'vue';

async function openSkillsFromMenu(): Promise<void> {
  insertMenuRef.value?.hide?.();
  await nextTick();  // 等 Popover 关闭
  openTrigger('skill');
}

async function openCommandsFromMenu(): Promise<void> {
  insertMenuRef.value?.hide?.();
  await nextTick();
  openTrigger('slash');
}
```

**步骤 4：确认 InputTriggerPanel 的 z-index 高于 Popover 残留**

**文件：`client/app/components/chat/InputTriggerPanel.vue:255`**

当前 `z-index: 80`。PrimeVue Popover 的默认 z-index 在 PrimeVue 的 DynamicPopper 层（通常 1000+）。如果 Popover 的关闭动画未完成，其 z-index 可能高于 InputTriggerPanel。

修复：将 InputTriggerPanel 的 z-index 提升到 Popover 同级或更高：

```scss
.input-trigger-panel {
  position: fixed;
  z-index: 1100;  /* ← 从 80 提升到 1100，确保高于 Popover 残留 */
  ...
}
```

### 5.3 验证清单

修改后需验证：
1. `scoreTriggerMatch` 函数当前在空 query 时的返回值（确认是否是根因）。
2. PrimeVue Popover 的 `hide()` 是否同步还是异步。
3. `--p-surface-100` 在暗色模式 Popover 内的实际计算值。

### 5.4 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/components/chat/inputBox.vue:884-891` | `<code>` 背景改 `color-mix(currentColor 12%, transparent)` |
| 修改 | `client/app/components/chat/inputBox.vue:432-440` | `openSkillsFromMenu`/`openCommandsFromMenu` 加 `await nextTick()` |
| 修改 | `client/app/components/chat/InputTriggerPanel.vue:255` | z-index 从 80 提升到 1100 |
| 检查/修改 | `client/app/composables/use-input-trigger.ts` | `scoreTriggerMatch` 空 query 返回 0（需先确认现状） |

### 5.5 测试

- 暗色模式：加号菜单中 `<code>$</code>` / `<code>/</code>` / `<code>@</code>` 不再有白色框，改为半透明底色。
- 亮色模式：标签底色仍可见但不突兀。
- 点击"技能" → InputTriggerPanel 面板出现，显示完整技能列表（非空）。
- 点击"命令" → InputTriggerPanel 面板出现，显示完整命令列表（非空）。
- 点击"文件" → 左栏文件树展开（行为不变）。
- 面板定位在输入框上方，不被遮挡。
- Popover 关闭动画与 InputTriggerPanel 出现无视觉冲突。

---


---

## 6. 修改加号菜单"文件"项 — 改为弹出文件选择器而非左侧文件树

> 目标：点击输入框加号菜单中的"文件"项时，弹出 OS 文件选择器（`<input type="file">`），将选中的文件作为附件添加到消息中，而非展开左侧文件树。参照 ZCode 的 `openFileDialog` + 隐藏 `<input type="file">` 模式。

### 6.1 ZCode 的做法（参照实现）

**文件：`D:\selfProj\ZCode\packages\ui\src\components\ai-elements\prompt-input.tsx:681-690`**

ZCode 在 PromptInput 组件内渲染一个**隐藏的 `<input type="file">`**：

```tsx
<input
  accept={accept}
  aria-label="Upload files"
  className="hidden"
  multiple={multiple}
  onChange={handleChange}
  ref={inputRef}
  title="Upload files"
  type="file"
/>
```

**触发文件选择器**（`prompt-input-actions.tsx:109-128`）：

```tsx
export const PromptInputActionAddAttachments = ({ label = "Add photos or files", ...props }) => {
  const attachments = usePromptInputAttachments();
  const handleSelect = useCallback((e: Event) => {
    e.preventDefault();
    attachments.openFileDialog();  // → inputRef.current?.click()
  }, [attachments]);
  return (
    <DropdownMenuItem {...props} onSelect={handleSelect}>
      <ImageIcon className="mr-2 size-4" /> {label}
    </DropdownMenuItem>
  );
};
```

**`openFileDialog` 的实现**（`prompt-input.tsx:189-191,338-340`）：

```tsx
const openFileDialog = useCallback(() => {
  openRef.current?.();  // → () => inputRef.current?.click()
}, []);

const openFileDialogLocal = useCallback(() => {
  inputRef.current?.click();  // 原生: 触发 OS 文件选择对话框
}, []);
```

**文件选中后的处理**（`prompt-input.tsx:131-147`）：

```tsx
const add = useCallback((files: File[] | FileList) => {
  const incoming = [...files];
  if (incoming.length === 0) return;
  setAttachmentFiles((prev) => [
    ...prev,
    ...incoming.map((file) => ({
      filename: file.name,
      id: nanoid(),
      mediaType: file.type,
      type: "file" as const,
      url: URL.createObjectURL(file),  // 创建 blob URL 供预览
    })),
  ]);
}, []);
```

**核心模式**：隐藏 `<input type="file">` → `inputRef.click()` 触发 OS 对话框 → `onChange` 拿到 `FileList` → 创建 blob URL → 加入附件列表 → UI 显示预览。

### 6.2 Sherry 当前做法与问题

**文件：`client/app/components/chat/inputBox.vue:442-448`**

```ts
/** Files keep their existing left-sidebar tree; the + entry reveals it. */
function openFilesFromMenu(): void {
  insertMenuRef.value?.hide?.();
  ui.sidebarBody = 'files';       // ← 切换左栏为文件树视图
  ui.filesSectionOpen = true;      // ← 展开文件树区域
  ui.sidebarCollapsed = false;     // ← 展开左栏
}
```

当前点击"文件"项是展开左侧文件树（供用户在树中浏览并 `@` 引用文件路径），而非弹出文件选择器上传文件作为附件。这与 ZCode 的做法完全不同——ZCode 是弹出 OS 文件选择器，选中的文件成为消息附件。

**Sherry 已有的附件基础设施**（`[sid].vue:241-264`）：

Sherry 已有三个隐藏的 `<input type="file">`（图片/音频/视频），各自有 `@change` handler（`onImageSelected`/`onAudioSelected`/`onVideoSelected`）和 trigger 函数（`triggerImagePicker`/`triggerAudioPicker`/`triggerVideoPicker`），选中后文件被转为 base64 加入 `selectedImages` 数组。

### 6.3 修改方案

将加号菜单中的"文件"项从"展开左侧文件树"改为"弹出 OS 文件选择器"，复用 Sherry 已有的隐藏 `<input type="file">` + 附件处理机制。

#### 6.3.1 新增一个通用的文件附件 input

**文件：`client/app/pages/home/index/[sid].vue`**

在已有的三个隐藏 input（图片/音频/视频）旁，新增一个通用文件 input（不限类型）：

```html
<!-- Hidden generic file input: triggered by the + menu's file entry -->
<input
  ref="fileAttachmentInputRef"
  type="file"
  multiple
  class="hidden"
  @change="onFileAttachmentSelected" />
```

#### 6.3.2 新增文件附件处理逻辑

**文件：`client/app/pages/home/index/[sid].vue`**

```ts
const fileAttachmentInputRef = useTemplateRef<HTMLInputElement>('fileAttachmentInputRef');

/** Trigger the OS file picker for generic file attachments. */
function triggerFilePicker(): void {
  fileAttachmentInputRef.value?.click();
}

/** Handle file selection: convert to base64 and add to pending images/attachments. */
function onFileAttachmentSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  const files = input.files;
  if (!files || files.length === 0) return;
  for (const file of Array.from(files)) {
    // 复用现有的 addImage 基础设施（base64 转换 + 10 张上限检查）
    if (selectedImages.value.length >= MAX_PENDING_IMAGES) break;
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = (reader.result as string)?.split(',')[1] ?? '';
      selectedImages.value = [
        ...selectedImages.value,
        { base64, name: file.name }
      ];
    };
    reader.readAsDataURL(file);
  }
  // 重置 input value 允许重复选择同一文件
  input.value = '';
}
```

**注意**：Sherry 当前的附件管线是 `selectedImages`（base64 数组，上限 10），图片/音频/视频都走同一路径。通用文件也复用此管线——非图片文件作为 base64 附件发送，后端处理多模态消息。如果后续需要区分文件类型展示不同预览，可扩展 `selectedImages` 的数据结构加 `mediaType` 字段。

#### 6.3.3 修改 inputBox.vue 的 openFilesFromMenu

**文件：`client/app/components/chat/inputBox.vue:442-448`**

将"文件"项的 handler 从展开左栏文件树改为 emit 事件触发文件选择器：

```ts
// 新增 emit
const emit = defineEmits<{
  (e: 'send', ...): void;
  (e: 'stop'): void;
  (e: 'uploadImage'): void;   // ← 第 4 节新增
  (e: 'uploadAudio'): void;   // ← 第 4 节新增
  (e: 'uploadVideo'): void;   // ← 第 4 节新增
  (e: 'uploadFile'): void;     // ← 新增：通用文件附件
}>();

/** Files: open the OS file picker (ZCode pattern), not the sidebar tree. */
function openFilesFromMenu(): void {
  insertMenuRef.value?.hide?.();
  emit('uploadFile');  // → 父组件调 triggerFilePicker()
}
```

**移除**：`ui.sidebarBody = 'files'` / `ui.filesSectionOpen = true` / `ui.sidebarCollapsed = false` 这三行不再需要。

#### 6.3.4 修改 [sid].vue 绑定

**文件：`client/app/pages/home/index/[sid].vue`**

ChatInputBox 绑定新增 `@upload-file`：

```html
<ChatInputBox
  ...
  @upload-image="triggerImagePicker"
  @upload-audio="triggerAudioPicker"
  @upload-video="triggerVideoPicker"
  @upload-file="triggerFilePicker" />
```

#### 6.3.5 加号菜单"文件"项的图标和文案

**文件：`client/app/components/chat/inputBox.vue:52-59`**

修改图标从 `pi-folder` 改为 `pi-paperclip`（附件语义），快捷键标签 `@` 改为空或移除（文件选择器不再对应 `@` 语法）：

```html
<button type="button" class="insert-item" @click="openFilesFromMenu">
  <span class="pi pi-paperclip" />
  <span>{{ t('chatInput.uploadFile') }}</span>
</button>
```

移除 `<code>@</code>` 标签——文件项不再关联 `@` 触发符（`@` 文件引用仍可通过直接在输入框输入 `@` 触发，那是独立的 `use-file-mention` 功能，不受此改动影响）。

### 6.4 与第 4 节（合并 MediaMenu）的关系

第 4 节计划将 MediaMenu 的图片/音频/视频上传合并到加号 Popover 菜单。本节进一步将"文件"项也改为文件选择器。合并后加号菜单最终形态：

```
┌─────────────────────────┐
│ 🖼️  图片上传             │  ← emit('uploadImage')  → triggerImagePicker()
│ 🎵  音频上传             │  ← emit('uploadAudio')  → triggerAudioPicker()
│ 🎬  视频上传             │  ← emit('uploadVideo')  → triggerVideoPicker()
│ ─────────────────────── │
│ 📎  添加文件             │  ← emit('uploadFile')   → triggerFilePicker()  ← 本节新增
│ ⚡  技能        $        │  ← openTrigger('skill')
│ /  命令        /        │  ← openTrigger('slash')
└─────────────────────────┘
```

"文件"项与"图片/音频/视频"项的区别：
- 图片/音频/视频：`accept` 限定类型，选中后走 `onImageSelected` 等专用 handler。
- 文件（通用）：不限 `accept`，选中后走 `onFileAttachmentSelected`，任意类型文件均可作为附件。

### 6.5 注意事项

| 事项 | 说明 |
|------|------|
| `@` 文件引用 | 输入框直接输入 `@` 仍触发 `use-file-mention`（左栏文件树拖拽 / `@path` 引用），不受本改动影响。加号菜单的"文件"项只是不再展开左栏树。 |
| 非图片文件预览 | `selectedImages` 数组中非图片文件（如 .pdf/.txt）在前端预览区可能显示为破图。需在预览区根据文件类型显示通用文件图标——这是后续优化项，不阻塞本次改动。 |
| 文件大小限制 | 现有 `MAX_PENDING_IMAGES`（10 张）上限适用于所有附件。大文件 base64 转换可能卡顿——后续可改为 blob URL（ZCode 做法），但需后端配合。 |
| `ui.sidebarBody` | 左栏文件树功能本身不受影响——工具栏的文件夹按钮（`pi pi-folder`，`index.vue:195-208`）仍可切换左栏为文件视图。 |

### 6.6 i18n 新增

```json
{
  "chatInput": {
    "uploadFile": "添加文件 / Add file / ファイルを追加 / 파일 추가"
  }
}
```

四语言同步。

### 6.7 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/components/chat/inputBox.vue` | `openFilesFromMenu` 改为 `emit('uploadFile')`；图标改 `pi-paperclip`；移除 `<code>@</code>` |
| 修改 | `client/app/pages/home/index/[sid].vue` | 新增隐藏 `<input type="file">` + `triggerFilePicker` + `onFileAttachmentSelected`；ChatInputBox 绑定 `@upload-file` |
| 修改 | i18n | 新增 `chatInput.uploadFile` |

### 6.8 测试

- 点击加号 → "添加文件" → 弹出 OS 文件选择器（非左栏文件树）。
- 选择文件 → 文件加入附件预览区 → 发送时作为附件上传。
- 选择多个文件 → 全部加入（受 10 个上限）。
- 重复选择同一文件 → 可正常选择（input value 已重置）。
- 输入框直接输入 `@` → 仍触发文件引用（`use-file-mention`），不受影响。
- 工具栏文件夹按钮 → 仍可展开左栏文件树，不受影响。

---


---

## 7. 左栏 Git Graph tab 改名为 "Shadow Git Graph"

> 目标：选中项目文件夹后，左侧栏出现的"文件树"和"Git Graph"两个 section tab 中，将"Git Graph"改名为"Shadow Git Graph"。

### 7.1 现状分析

**文件：`client/app/pages/home/components/SessionSidebar.vue:427-444`**

左栏的文件树/Git Graph 两个 section header 通过 `sidebarSections` computed 渲染：

```ts
const sidebarSections = computed(() => [
  { id: 'files', icon: 'pi pi-folder', label: 'projectFiles.title', open: ui.filesSectionOpen, toggle: ... },
  { id: 'git',   icon: 'pi pi-sitemap', label: 'gitGraph.title',   open: ui.gitSectionOpen,   toggle: ... }
]);
```

模板中通过 `t(section.label)` 渲染标题（第 77 行）：`<span class="truncate">{{ t(section.label) }}</span>`，即 `t('gitGraph.title')`。

**`gitGraph.title` 的 i18n 值定义在两处：**

1. **全局 locales**：`client/app/i18n/locales/{zh,en,ja,ko}.json` — 四个文件均在 `gitGraph.title` 处定义为 `"Git Graph"`。
2. **组件级 i18n**：`client/app/pages/home/components/GitGraphPanel.vue:719-813` — `<i18n lang="json">` 块内四语言也各自定义 `gitGraph.title` 为 `"Git Graph"`。

两处的 `gitGraph.title` 当前值均为 `"Git Graph"`（四语言统一）。

### 7.2 修改方案

将两处 `gitGraph.title` 的值从 `"Git Graph"` 改为 `"Shadow Git Graph"`。

#### 7.2.1 全局 locales（4 个文件）

| 文件 | 行号 | 修改前 | 修改后 |
|------|------|--------|--------|
| `client/app/i18n/locales/zh.json:683` | `"title": "Git Graph"` | `"title": "Shadow Git Graph"` |
| `client/app/i18n/locales/en.json:683` | `"title": "Git Graph"` | `"title": "Shadow Git Graph"` |
| `client/app/i18n/locales/ja.json:683` | `"title": "Git Graph"` | `"title": "Shadow Git Graph"` |
| `client/app/i18n/locales/ko.json:683` | `"title": "Git Graph"` | `"title": "Shadow Git Graph"` |

#### 7.2.2 组件级 i18n（GitGraphPanel.vue）

| 文件 | 行号 | 修改前 | 修改后 |
|------|------|--------|--------|
| `client/app/pages/home/components/GitGraphPanel.vue:723` | `"title": "Git Graph"` (zh) | `"title": "Shadow Git Graph"` |
| `client/app/pages/home/components/GitGraphPanel.vue:746` | `"title": "Git Graph"` (en) | `"title": "Shadow Git Graph"` |
| `client/app/pages/home/components/GitGraphPanel.vue:769` | `"title": "Git Graph"` (ja) | `"title": "Shadow Git Graph"` |
| `client/app/pages/home/components/GitGraphPanel.vue:792` | `"title": "Git Graph"` (ko) | `"title": "Shadow Git Graph"` |

### 7.3 注意事项

- `gitGraph.title` 仅用于左栏 section header 标题（`SessionSidebar.vue:77` 的 `t(section.label)`），不涉及 GitGraphPanel 内部的其他文案（refresh/dirty/empty 等），这些 key 不变。
- 名称 "Shadow Git Graph" 是因为该面板读取的是 sherry 的 shadow git 检查点仓库（`APP_HOME/checkpoints/store` 的 `refs/sherry/<hash>` 分支），而非用户项目的原生 git 仓库——改名后更准确地反映数据来源。
- 四语言统一用 `"Shadow Git Graph"`（英文名，不翻译），与 "Git Graph" 原来的处理方式一致（原来四语言也都是英文 "Git Graph" 未翻译）。

### 7.4 文件清单

| 操作 | 文件 |
|------|------|
| 修改 | `client/app/i18n/locales/zh.json`（`gitGraph.title`） |
| 修改 | `client/app/i18n/locales/en.json`（`gitGraph.title`） |
| 修改 | `client/app/i18n/locales/ja.json`（`gitGraph.title`） |
| 修改 | `client/app/i18n/locales/ko.json`（`gitGraph.title`） |
| 修改 | `client/app/pages/home/components/GitGraphPanel.vue`（组件级 i18n 块，四语言 `gitGraph.title`） |

### 7.5 测试

- 选中项目文件夹 → 左栏出现两个 section tab → 第二个 tab 标题显示 "Shadow Git Graph"。
- 四种语言下标题均为 "Shadow Git Graph"。
- GitGraphPanel 内部其他文案（刷新/未提交/暂无提交等）不变。

---


---

## 8. 修复系统设置-记忆设置 tab 页内容为空

> 目标：系统设置 → 记忆设置 tab 打开后应显示记忆容量上限/语义召回/整理等表单，而非空白。

### 8.1 根因

**文件：`client/app/pages/home/components/ConfigPanel.vue:433-451`（script imports）+ `:384`（template）**

`ConfigPanel.vue` 的 `<script setup>` 导入了所有同目录子组件——`SafetyFilterPanel`（448）、`ShortcutSettingsSection`（446）、`DesktopSettingsSection`（447）、`LlmModelManager`（442）等——**唯独缺少 `MemorySettingsSection` 的 import**：

```ts
// ConfigPanel.vue:433-451 — 当前 import 列表
import LlmModelManager from './LlmModelManager.vue';
import CudaPackCard from './CudaPackCard.vue';
import ReplayAllowlistEditor from './ReplayAllowlistEditor.vue';
import I18nPanel from './I18nPanel.vue';
import ShortcutSettingsSection from './ShortcutSettingsSection.vue';
import DesktopSettingsSection from './DesktopSettingsSection.vue';
import SafetyFilterPanel from './SafetyFilterPanel.vue';
// ← 缺少: import MemorySettingsSection from './MemorySettingsSection.vue';
```

模板第 384 行使用了 `<MemorySettingsSection />`：

```html
<TabPanel value="memorySettings" :header="t('config.tabs.memorySettings')">
  <MemorySettingsSection />   <!-- ← 组件未注册，渲染为空 -->
</TabPanel>
```

**为什么是空的**：Nuxt 的默认组件自动导入只扫描 `app/components/` 目录，不扫描 `app/pages/home/components/`（这是页面级子目录，不是全局组件目录）。`nuxt.config.ts` 中无 `components` 配置项扩展扫描路径。因此 `MemorySettingsSection` 不会被自动注册——Vue 将其视为未知自定义元素，渲染为空标签 `<memorysettingssection>`（不产生可见内容）。

其他同目录组件（`SafetyFilterPanel` 等）都有显式 import 所以正常工作，唯独 `MemorySettingsSection` 遗漏了。

### 8.2 修改方案

**在 `ConfigPanel.vue` 的 `<script setup>` import 列表中补充 `MemorySettingsSection` 的导入。**

**文件：`client/app/pages/home/components/ConfigPanel.vue`**

在 `SafetyFilterPanel` import 之后（第 448 行后）追加：

```ts
import SafetyFilterPanel from './SafetyFilterPanel.vue';
import MemorySettingsSection from './MemorySettingsSection.vue';  // ← 新增
```

### 8.3 验证

- `MemorySettingsSection.vue` 文件存在于 `client/app/pages/home/components/`（已确认）。
- 组件本身完整——有 `onMounted(load)` 加载设置、模板渲染表单字段、语义召回开关、整理按钮，完整 UI（已确认）。
- 只需补一行 import 即可修复。

### 8.4 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/pages/home/components/ConfigPanel.vue:448` | 追加 `import MemorySettingsSection from './MemorySettingsSection.vue'` |

### 8.5 测试

- 系统设置 → 记忆设置 tab → 显示完整的记忆设置表单（容量上限、语义召回、整理按钮）。
- 表单字段加载后显示后端返回的当前值（`onMounted(load)` → `readMemorySettings()`）。
- 保存按钮 → `writeMemorySettings` 正常工作。
- 整理按钮 → 需要会话（`sessionId` 来自 route params）。

---


---

## 9. 将系统设置-记忆设置移到菜单-记忆面板内

> 目标：将 `MemorySettingsSection` 的配置内容（容量上限/语义召回/整理按钮）从系统设置 ConfigPanel 移到菜单-记忆 `MemoryPanel` 中，作为记忆面板顶部的一个可折叠配置区，减少入口分散。

### 9.1 现状

- **菜单-记忆**（`MemoryPanel.vue`，右栏 tab）：记忆条目 CRUD + 容量统计 + 搜索/排序/归档。
- **系统设置-记忆设置**（`MemorySettingsSection.vue`，ConfigPanel 内 tab）：容量上限调节 + 语义召回开关 + 整理按钮。
- 两者互补但不分散入口——用户管理记忆时自然需要同时看到配置。当前配置藏在系统设置深处，且因 import 缺失一直为空（第 8 节）。

### 9.2 修改方案

将 `MemorySettingsSection` 的配置内容**内联到 `MemoryPanel.vue` 顶部**，作为容量统计区下方、条目列表上方的可折叠配置区。同时从 `ConfigPanel.vue` 移除记忆设置 tab。

#### 9.2.1 修改 MemoryPanel.vue — 内联配置区

**文件：`client/app/pages/home/components/MemoryPanel.vue`**

在容量统计区（`targetStats`，第 30-51 行）下方、搜索工具栏（第 53 行）上方，插入一个可折叠的配置区：

```html
<!-- 记忆设置区：容量上限 / 语义召回 / 整理，从 ConfigPanel 移入 -->
<div class="shrink-0">
  <button
    type="button"
    class="flex w-full items-center gap-1.5 py-1 text-xs text-gray-500 dark:text-gray-400"
    @click="showSettings = !showSettings">
    <i class="pi text-[10px]" :class="showSettings ? 'pi-chevron-down' : 'pi-chevron-right'" />
    {{ t('memoryPanel.settings') }}
  </button>

  <div v-if="showSettings" class="flex flex-col gap-3 rounded-lg border border-gray-100 p-3 dark:border-gray-800">
    <!-- 容量上限 -->
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.memoryLimit') }}
        </label>
        <InputNumber v-model="settingsForm.memory_char_limit" :min="1" :max="8000"
          :use-grouping="false" class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.userLimit') }}
        </label>
        <InputNumber v-model="settingsForm.user_char_limit" :min="1" :max="8000"
          :use-grouping="false" class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.factsLimit') }}
        </label>
        <InputNumber v-model="settingsForm.facts_char_limit" :min="1" :max="8000"
          :use-grouping="false" class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.maxUnusedDays') }}
        </label>
        <InputNumber v-model="settingsForm.max_unused_days" :min="1" :max="3650"
          :use-grouping="false" class="w-full" />
      </div>
    </div>

    <!-- 语义召回 -->
    <div class="flex flex-col gap-2 rounded-lg border border-dashed border-gray-200 p-2 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <ToggleSwitch v-model="settingsForm.semantic_recall_enabled" data-test="memory-semantic-toggle" />
        <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.semanticRecall') }}
        </span>
      </div>
      <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div class="flex flex-col gap-1">
          <label class="text-xs text-gray-600 dark:text-gray-300">{{ t('memorySettings.semanticTopK') }}</label>
          <InputNumber v-model="settingsForm.semantic_recall_top_k" :min="1" :max="20"
            :use-grouping="false" :disabled="!settingsForm.semantic_recall_enabled" class="w-full" />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-xs text-gray-600 dark:text-gray-300">{{ t('memorySettings.semanticBudget') }}</label>
          <InputNumber v-model="settingsForm.semantic_recall_char_budget" :min="200" :max="20000"
            :use-grouping="false" :disabled="!settingsForm.semantic_recall_enabled" class="w-full" />
        </div>
      </div>
    </div>

    <!-- 保存 + 整理按钮 -->
    <div class="flex flex-wrap items-center gap-2">
      <Button :label="t('memorySettings.save')" icon="pi pi-check" size="small"
        :loading="savingSettings" :disabled="!settingsDirty" data-test="memory-settings-save"
        @click="handleSaveSettings" />
      <Button :label="t('memorySettings.consolidate')" icon="pi pi-filter-slash" size="small"
        severity="secondary" :loading="consolidating" data-test="memory-consolidate"
        @click="handleConsolidate" />
      <span v-if="consolidationMessage" class="text-xs text-gray-500 dark:text-gray-400">
        {{ consolidationMessage }}
      </span>
    </div>
  </div>
</div>
```

**script 部分**：将 `MemorySettingsSection.vue` 的所有逻辑（`form`/`original`/`dirty`/`load`/`handleSave`/`handleConsolidate`/`DEFAULT_FORM`/`apply`）内联到 `MemoryPanel.vue` 的 `<script setup>` 中，并在 `onMounted` 时与条目加载一起执行。

```ts
// 新增到 MemoryPanel.vue <script setup>
import { readMemorySettings, writeMemorySettings, consolidateMemory, readConsolidationStatus } from '~/composables/bridge';

const showSettings = ref(false);
const settingsForm = ref({ ...DEFAULT_FORM });
const settingsOriginal = ref({ ...DEFAULT_FORM });
const savingSettings = ref(false);
const consolidating = ref(false);
const consolidationMessage = ref('');

const settingsDirty = computed(() =>
  (Object.keys(DEFAULT_FORM) as (keyof typeof DEFAULT_FORM)[]).some(
    key => settingsForm.value[key] !== settingsOriginal.value[key]
  )
);

async function loadSettings() {
  try {
    const settings = await readMemorySettings();
    settingsForm.value = { /* ...映射... */ };
    settingsOriginal.value = { ...settingsForm.value };
  } catch (e) {
    logUtil.e('[MemoryPanel] Failed to load settings:', e);
  }
}

async function handleSaveSettings() { /* ...原 MemorySettingsSection 逻辑... */ }
async function handleConsolidate() { /* ...原 MemorySettingsSection 逻辑... */ }

// onMounted 中同时加载条目和设置
onMounted(() => {
  void load();
  void loadSettings();
});
```

#### 9.2.2 修改 ConfigPanel.vue — 移除记忆设置 tab

**文件：`client/app/pages/home/components/ConfigPanel.vue`**

1. 移除 `MemorySettingsSection` 的 `TabPanel`（第 378-385 行）：

```html
<!-- 移除整个 TabPanel -->
<!-- <TabPanel value="memorySettings" :header="t('config.tabs.memorySettings')">
  <MemorySettingsSection />
</TabPanel> -->
```

2. 移除 i18n 中的 `memorySettings` tab 标签（`config.tabs.memorySettings`，第 1136 行等四处）。
3. 移除 footer 的 `activeTab !== 6` 条件（第 406 行，memorySettings tab 不再存在）。
4. 更新后续 tab 的 index（desktop tab 从 7 变 6）。

**注意**：`activeTab` 的 index 引用在 `watch`（826 行）、`canSave`（861 行）、`onHide`（1034 行）、footer `v-if`（406 行）中——移除一个 tab 后需同步更新所有硬编码 index。建议改用 `value` 而非 index 驱动（PrimeVue TabView 支持 `v-model:activeIndex` + `TabPanel value="..."` 但 active 仍按 index），或谨慎更新 index。

#### 9.2.3 删除 MemorySettingsSection.vue

**删除文件：`client/app/pages/home/components/MemorySettingsSection.vue`**

功能已完全内联到 `MemoryPanel.vue`，该组件不再被引用。

**同步删除测试文件（如有）：**
- `client/app/pages/home/components/__tests__/MemorySettingsSection.integration.test.ts`（如存在）

#### 9.2.4 i18n 调整

- `MemoryPanel.vue` 的 `<i18n>` 块中新增 `memorySettings.*` 命名空间（从 `MemorySettingsSection.vue` 搬移）。
- 新增 `memoryPanel.settings` 标签（如"记忆设置"/"Memory settings"）。
- `ConfigPanel.vue` 的 i18n 中移除 `config.tabs.memorySettings`。

### 9.3 注意事项

| 事项 | 说明 |
|------|------|
| tab index | 移除 ConfigPanel 的 memorySettings tab 后，desktop tab 的 index 从 7→6，需同步更新 footer `v-if`、`watch`、`canSave`、`onHide` 中的硬编码 index |
| 整理按钮的 sessionId | `MemorySettingsSection` 原 `handleConsolidate` 需要 `route.params.sid`，`MemoryPanel` 也通过 route 获取 sessionId（右栏 tab 的 payload 携带 sessionId），需确认 sessionId 来源一致 |
| 配置区折叠 | 默认折叠（`showSettings = false`），不占记忆面板空间；用户需要时点开 |
| 统计刷新 | 保存设置后需刷新 `stats`（容量进度条依赖上限值），`handleSaveSettings` 中调 `load()` 重新加载统计 |
| 组件体积 | `MemoryPanel.vue` 会增大，但逻辑内聚——记忆管理 + 配置在同一面板 |

### 9.4 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/pages/home/components/MemoryPanel.vue` | 内联配置区模板 + script 逻辑 + i18n |
| 修改 | `client/app/pages/home/components/ConfigPanel.vue` | 移除 memorySettings TabPanel + 更新 tab index + 移除 i18n |
| 删除 | `client/app/pages/home/components/MemorySettingsSection.vue` | 功能已内联 |

### 9.5 测试

- 菜单 → 记忆 → 面板顶部出现"记忆设置"折叠区，点开展示容量上限/语义召回/整理按钮。
- 修改容量上限 → 保存 → 容量统计进度条刷新。
- 语义召回开关 → 保存 → 生效。
- 整理按钮 → 需要会话 → 执行 consolidation → 显示归档结果。
- 系统设置 → 不再有"记忆设置"tab。
- 系统设置的其他 tab（背景/环境/快捷键/安全过滤/桌面）不受影响。

---


---

## 12. 模型配置字段名中文化 — PROVIDER/NAME/API_BASE/ENABLE_THINKING/REASONING_EFFORT 等字段加中文标签和说明

> 目标：模型配置中 `_PROVIDER`、`_NAME`、`_API_BASE`、`_ENABLE_THINKING`、`_REASONING_EFFORT`、`_API_KEY` 等字段当前直接显示英文环境变量名，用户看不懂。参照已有的"上下文窗口（MAX_TOKEN）"和"最大输出窗口（MAX_OUTPUT_TOKEN）"模式——既显示中文标签，又在下方说明字段含义。

### 12.1 现状分析

**文件：`client/app/pages/home/components/LlmModelManager.vue:456-461`**

```ts
const paramLabel = (key: string): string => {
  if (isPenaltyKey(key)) return penaltyLabel(key);
  if (key.endsWith('_MAX_OUTPUT_TOKEN')) return t('config.llm.maxOutputLabel');
  if (key.endsWith('_MAX_TOKEN')) return t('config.llm.maxContextLabel');
  return key;  // ← 其他所有字段直接返回原始 env key
};
```

`paramLabel` 只对三类字段返回本地化标签：
- `_FREQUENCY_PENALTY` / `_PRESENCE_PENALTY` → "频率惩罚（frequency_penalty）" / "存在惩罚（presence_penalty）"
- `_MAX_OUTPUT_TOKEN` → "最大输出窗口（MAX_OUTPUT_TOKEN）"
- `_MAX_TOKEN` → "上下文窗口（MAX_TOKEN）"

其余所有字段都直接返回原始 env key（如 `MAIN_LLM_PROVIDER`、`MAIN_LLM_NAME`、`MAIN_LLM_API_BASE`、`MAIN_LLM_ENABLE_THINKING`、`MAIN_LLM_REASONING_EFFORT`、`MAIN_LLM_API_KEY` 等），用户看到的是纯英文大写字符串。

### 12.2 已有的良好模式（参照标准）

```
上下文窗口（MAX_TOKEN）
必须 >= 200000 (200K)，否则 Agent 将拒绝启动。

最大输出窗口（MAX_OUTPUT_TOKEN）
需 ≥ 8192 且小于对应模型上下文窗口；留空使用提供商默认。
```

每个字段：
1. **标签**：中文说明 + 括号内保留原始 env key（方便对照 .env 文件）
2. **提示**：字段下方一行小字说明含义/约束/留空行为

### 12.3 修改方案

#### 12.3.1 扩展 `paramLabel` 函数

**文件：`client/app/pages/home/components/LlmModelManager.vue:456-461`**

```ts
const paramLabel = (key: string): string => {
  if (isPenaltyKey(key)) return penaltyLabel(key);
  if (key.endsWith('_MAX_OUTPUT_TOKEN')) return t('config.llm.maxOutputLabel');
  if (key.endsWith('_MAX_TOKEN')) return t('config.llm.maxContextLabel');
  // 新增：通用字段标签
  if (key.endsWith('_PROVIDER')) return t('config.llm.providerLabel');
  if (key.endsWith('_NAME')) return t('config.llm.nameLabel');
  if (key.endsWith('_API_BASE')) return t('config.llm.apiBaseLabel');
  if (key.endsWith('_API_KEY')) return t('config.llm.apiKeyLabel');
  if (key.endsWith('_ENABLE_THINKING')) return t('config.llm.enableThinkingLabel');
  if (key.endsWith('_REASONING_EFFORT')) return t('config.llm.reasoningEffortLabel');
  if (key.endsWith('_TEMPERATURE')) return t('config.llm.temperatureLabel');
  if (key.endsWith('_TOP_P')) return t('config.llm.topPLabel');
  if (key.endsWith('_MAX_RETRIES')) return t('config.llm.maxRetriesLabel');
  if (key.endsWith('_TIMEOUT')) return t('config.llm.timeoutLabel');
  return key;
};
```

#### 12.3.2 新增字段提示函数

参照 `maxTokenHint`（第 117 行）和 `maxOutputHint`（第 127 行）的模式，新增一个 `paramHint(key)` 函数，返回字段下方的说明文字：

```ts
const paramHint = (key: string): string => {
  if (key.endsWith('_PROVIDER')) return t('config.llm.providerHint');
  if (key.endsWith('_NAME')) return t('config.llm.nameHint');
  if (key.endsWith('_API_BASE')) return t('config.llm.apiBaseHint');
  if (key.endsWith('_API_KEY')) return t('config.llm.apiKeyHint');
  if (key.endsWith('_ENABLE_THINKING')) return t('config.llm.enableThinkingHint');
  if (key.endsWith('_REASONING_EFFORT')) return t('config.llm.reasoningEffortHint');
  if (key.endsWith('_TEMPERATURE')) return t('config.llm.temperatureHint');
  if (key.endsWith('_TOP_P')) return t('config.llm.topPHint');
  if (key.endsWith('_MAX_RETRIES')) return t('config.llm.maxRetriesHint');
  if (key.endsWith('_TIMEOUT')) return t('config.llm.timeoutHint');
  return '';
};
```

#### 12.3.3 模板中渲染字段提示

**文件：`client/app/pages/home/components/LlmModelManager.vue:104-130`**

在现有字段标签和提示之后，新增通用字段提示的渲染：

```html
<div
  v-for="key in paramKeys"
  :key="key"
  class="flex flex-col gap-1">
  <span
    class="text-xs text-gray-500 dark:text-gray-400"
    :title="key"
    >{{ paramLabel(key) }}</span>

  <!-- 已有：MAX_TOKEN 提示 -->
  <p
    v-if="guardedTokenKey(key) && !isLocalEntrySelected"
    class="m-0 text-xs text-amber-600 dark:text-amber-400">
    {{ t('config.env.maxTokenHint') }}
  </p>
  <!-- 已有：PENALTY 提示 -->
  <p
    v-if="isPenaltyKey(key) && !isLocalEntrySelected"
    class="m-0 text-xs text-gray-400 dark:text-gray-500">
    {{ t('config.llm.penaltyHint', { min: PENALTY_MIN, max: PENALTY_MAX }) }}
  </p>
  <!-- 已有：MAX_OUTPUT_TOKEN 提示 -->
  <p
    v-if="isOutputCapKey(key) && !isLocalEntrySelected"
    class="m-0 text-xs text-gray-400 dark:text-gray-500">
    {{ t('config.llm.maxOutputHint', { min: MIN_OUTPUT_TOKENS }) }}
  </p>

  <!-- 新增：通用字段提示 -->
  <p
    v-if="paramHint(key) && !isLocalEntrySelected"
    class="m-0 text-xs text-gray-400 dark:text-gray-500">
    {{ paramHint(key) }}
  </p>

  <!-- 输入框（已有逻辑不变） -->
  ...
</div>
```

#### 12.3.4 i18n 新增字段标签和提示

**文件：`client/app/i18n/locales/zh.json`（以及 en/ja/ko 同步）**

在 `config.llm` 命名空间中新增：

```json
{
  "config": {
    "llm": {
      "providerLabel": "提供商（PROVIDER）",
      "providerHint": "模型服务商标识，如 openai、anthropic、deepseek、dashscope 等；留空则从 .env 全局默认推断。",
      "nameLabel": "模型 API 名称（NAME）",
      "nameHint": "提供商侧的模型标识符，如 gpt-4o、claude-sonnet-4-20250514、deepseek-chat 等。",
      "apiBaseLabel": "API 地址（API_BASE）",
      "apiBaseHint": "自定义 API 入口地址（含 https://）；留空使用提供商默认地址。兼容 OpenAI 格式的第三方服务可填其 base URL。",
      "apiKeyLabel": "API 密钥（API_KEY）",
      "apiKeyHint": "调用该模型的鉴权密钥；留空则使用 .env 中同名全局密钥。",
      "enableThinkingLabel": "启用思考（ENABLE_THINKING）",
      "enableThinkingHint": "是否开启推理/思考模式（true/false）；开启后模型会先输出推理过程再给答案。",
      "reasoningEffortLabel": "推理强度（REASONING_EFFORT）",
      "reasoningEffortHint": "思考力度等级，如 low/medium/high；等级越高推理越深入但耗时更长。留空使用模型默认。",
      "temperatureLabel": "采样温度（TEMPERATURE）",
      "temperatureHint": "控制输出随机性，0=确定性、2=高随机；留空使用提供商默认（通常 1.0）。",
      "topPLabel": "核采样（TOP_P）",
      "topPHint": "核采样概率阈值（0~1）；留空使用提供商默认。与温度通常只调一个。",
      "maxRetriesLabel": "最大重试（MAX_RETRIES）",
      "maxRetriesHint": "请求失败后的自动重试次数；留空使用默认（0=不重试）。",
      "timeoutLabel": "超时秒数（TIMEOUT）",
      "timeoutHint": "单次请求超时时间（秒）；留空使用默认值。"
    }
  }
}
```

四语言同步（zh/en/ja/ko），翻译需保持技术术语一致。

### 12.4 文件清单

| 操作 | 文件 | 改动 |
|------|------|------|
| 修改 | `client/app/pages/home/components/LlmModelManager.vue` | `paramLabel` 扩展 + 新增 `paramHint` 函数 + 模板渲染提示 |
| 修改 | `client/app/i18n/locales/zh.json` | 新增 `config.llm.providerLabel` 等 ~11 对 label/hint |
| 修改 | `client/app/i18n/locales/en.json` | 同步英文 |
| 修改 | `client/app/i18n/locales/ja.json` | 同步日文 |
| 修改 | `client/app/i18n/locales/ko.json` | 同步韩文 |

### 12.5 测试

- 系统设置 → 模型配置 → 展开 MAIN_LLM → 每个字段显示中文标签（如"提供商（PROVIDER）"）+ 下方说明。
- PROVIDER 字段下方："模型服务商标识，如 openai、anthropic、deepseek..."
- NAME 字段下方："提供商侧的模型标识符，如 gpt-4o..."
- ENABLE_THINKING 字段下方："是否开启推理/思考模式（true/false）..."
- REASONING_EFFORT 字段下方："思考力度等级，如 low/medium/high..."
- API_KEY 字段下方："调用该模型的鉴权密钥；留空则使用 .env 中同名全局密钥。"
- 已有的 MAX_TOKEN / MAX_OUTPUT_TOKEN / PENALTY 提示不受影响。
- 四语言下标签和提示正确显示。
- `title` 属性仍保留原始 env key（鼠标悬停可查看）。

---

