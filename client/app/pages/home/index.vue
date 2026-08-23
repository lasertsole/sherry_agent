<template>
  <div
    class="relative w-full h-full flex text-theme-main"
    :style="chatBackgroundStyle">
    <!-- 背景遮罩层：浅色=白/深色=黑，opacity 由「背景图片」tab 的 slider 控制，
         越满照片越被冲淡成纯白/纯黑直至完全遮蔽。置于内容之下（pointer-events-none
         不拦截交互），且背景图蒙在根容器背景上，下方内容仍在上层可选择。 -->
    <div
      v-if="backgroundOpacity > 0"
      class="absolute inset-0 pointer-events-none"
      :style="chatBackgroundOverlayStyle" />
    <!-- 左侧-历史记录区域（会话列表侧边栏）：独立组件，状态/逻辑已随组件抽出。
         折叠态由父组件工具栏按钮控制（v-model:collapsed 双向同步）；
         当前会话 id 由父组件 v-model:current-session-id 双向同步（父组件用于加载角色快照）。 -->
    <SessionSidebar
      v-model:collapsed="isSidebarCollapsed"
      v-model:current-session-id="currentSessionId" />

    <!-- 右侧-会话主体区域 -->
    <div class="relative flex flex-col flex-1 min-w-0 h-full bg-transparent dark:bg-transparent">
      <!-- 顶部工具栏：flex 两端对齐。折叠/展开历史按钮靠左，其余功能按钮全部靠右。
           按钮始终可见（折叠后侧边栏收起，此按钮仍停留在会话区左上角，可再次展开）。 -->
      <div
        class="flex items-center justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- 左侧：折叠/展开历史侧边栏 -->
        <Button
          :icon="isSidebarCollapsed ? 'pi pi-angle-double-right' : 'pi pi-angle-double-left'"
          :title="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          :aria-label="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          variant="text"
          class="text-theme-main"
          @click="toggleSidebar" />
        <!-- 右侧：原有功能按钮区 -->
        <div class="flex items-center gap-3">
          <ModeSwitch />
          <!-- 子 Agent 实时流程图开关 -->
          <Button
            icon="pi pi-sitemap"
            :title="t('flow.toggle')"
            :aria-label="t('flow.toggle')"
            variant="text"
            :class="isFlowPanelVisible ? 'text-theme-main' : ''"
            @click="toggleFlowPanel" />
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <!-- 语言切换：从系统配置-语言设置移至顶部工具栏，直接读写 vue-i18n locale。
                 地球图标（pi-globe）让不同语言用户都能直观识别这是语言切换控件。 -->
            <Select
              :model-value="locale"
              :options="languageOptions"
              option-label="name"
              option-value="code"
              class="w-40"
              size="small"
              aria-label="Language / 语言"
              @update:model-value="onLanguageChange">
              <template #value="slotProps">
                <span
                  v-if="slotProps.value"
                  class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t(`config.language.${slotProps.value}`) }}</span>
                </span>
                <span
                  v-else
                  class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t('config.language.zh') }}</span>
                </span>
              </template>
              <template #option="slotProps">
                <span>{{ t(`config.language.${slotProps.option.code}`) }}</span>
              </template>
            </Select>
            <!-- 通知入口：🔔 bell 图标 + 未读/合并计数红色徽标。点击打开通知弹窗并清除未读。 -->
            <div class="relative flex items-center">
              <Button
                icon="pi pi-bell"
                :title="t('toolbar.notification')"
                :aria-label="t('toolbar.notification')"
                variant="text"
                @click="handleOperate('headerBar', 'notification')" />
              <span
                v-if="notificationUnread > 0"
                class="absolute -top-0.5 -right-0.5 flex min-w-[18px] h-[18px] items-center justify-center rounded-full px-1 text-[10px] leading-none font-medium text-white bg-red-500"
                :title="t('toolbar.notification')">
                {{ notificationUnread > 99 ? '99+' : notificationUnread }}
              </span>
            </div>
            <!-- 日志入口：保留在顶部（无对应九宫格图标，不并入设置菜单） -->
            <Button
              icon="pi pi-history"
              :title="t('toolbar.logs')"
              :aria-label="t('toolbar.logs')"
              variant="text"
              @click="handleOperate('headerBar', 'logs')" />
            <!-- 设置菜单入口：三条横线按钮。其余功能（技能/知识图谱/系统配置/扩展）
                 全部从顶部移入此按钮弹幕出的大 dialog 九宫格。 -->
            <Button
              icon="pi pi-bars"
              :title="t('toolbar.settingsMenu')"
              :aria-label="t('toolbar.settingsMenu')"
              variant="text"
              @click="isSettingsMenuOpen = true" />
          </div>
        </div>
      </div>

      <!-- 设置菜单：大 dialog 居中显示，内含九宫格。每个功能为正方形区块，
           上方大图标 + 下方功能名。点击某项直接触发对应功能（弹窗/路由跳转）。 -->
      <Dialog
        v-model:visible="isSettingsMenuOpen"
        :header="t('toolbar.settingsMenu')"
        :modal="true"
        :closable="true"
        class="w-[min(90vw,720px)]">
        <div class="grid grid-cols-3 gap-4">
          <button
            v-for="tool in headerTools"
            :key="tool.event"
            type="button"
            class="flex flex-col items-center justify-center gap-3 w-full h-32 rounded-xl border border-solid border-gray-light dark:border-gray-dark bg-gray-50 dark:bg-gray-800 hover:border-theme-main hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer"
            :title="t(tool.title)"
            @click="handleMenuSelect(tool.event)">
            <i
              :class="[tool.icon, 'text-4xl! text-theme-main']" />
            <span class="text-base text-theme-main">{{ t(tool.toolName) }}</span>
          </button>
        </div>
      </Dialog>
      <!-- 会话主体：每个会话由 [sid].vue 渲染。以 route.params.sid 作为 page-key，
           每个会话获得独立的 KeepAlive 缓存槽，切换时原样恢复其草稿/滚动/流式/HITL 状态。
           `max` 上限：超过 N 个缓存槽时，KeepAlive 会按 LRU 淘汰最久未访问的槽，
           防止删除的非激活会话（其 page-key 不再被路由引用，但槽仍驻留内存）导致无界增长。 -->
      <div class="flex-1 min-h-0">
        <NuxtPage
          :page-key="route => String(route.params.sid ?? 'root')"
          :keepalive="{ max: KEEP_ALIVE_MAX }" />
      </div>
    </div>

    <!-- 右侧-子 Agent 实时流程图面板：可折叠/关闭，随会话切换实时更新 -->
    <div
      v-if="isFlowPanelVisible"
      :class="[
        'relative h-full overflow-hidden transition-all duration-300 border-l border-solid border-gray-light dark:border-gray-dark',
        isFlowCollapsed ? 'w-0 border-l-0' : 'w-[320px] md:w-[360px]'
      ]">
      <div class="w-[320px] md:w-[360px] h-full">
        <SubagentFlowGraph
          v-model:collapsed="isFlowCollapsed"
          v-model:current-session-id="currentSessionId"
          v-model:visible="isFlowPanelVisible"
          :selected-run-id="flowSelectedRunId" />
      </div>
    </div>

    <!-- 技能查看弹窗 -->
    <SkillsDialog v-model="showSkillsDialog" />

    <!-- 统计弹窗 -->
    <StatsDialog v-model="showStatsDialog" />

    <!-- 系统配置弹窗 -->
    <ConfigDialog
      v-model="showConfigDialog"
      @saved="loadCharacter" />

    <!-- AI人格弹窗 -->
    <PersonaDialog v-model="showPersonaDialog" />

    <!-- 记忆弹窗 -->
    <MemoryDialog v-model="showMemoryDialog" />

    <!-- 心跳任务弹窗 -->
    <HeartbeatDialog v-model="showHeartbeatDialog" />

    <!-- 定时任务弹窗 -->
    <CronDialog v-model="showCronDialog" />

    <!-- 日志查看弹窗 -->
    <LogsDialog v-model="showLogsDialog" />

    <!-- 通知查看弹窗（监听 ws:notification，合并连续相同通知，未读数经 changed 上报） -->
    <NotificationDialog
      v-model="showNotificationDialog"
      @changed="(n: number) => (notificationUnread = n)" />

    <!-- 扩展弹窗（关联 / mcp） -->
    <ExtendDialog v-model="showExtendDialog" />
  </div>
</template>

<script lang="ts" setup>
// components
import SessionSidebar from './components/SessionSidebar.vue';
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import SubagentFlowGraph from './components/SubagentFlowGraph.vue';
import SkillsDialog from './components/SkillsDialog.vue';
import StatsDialog from './components/StatsDialog.vue';
import ConfigDialog from './components/ConfigDialog.vue';
import PersonaDialog from './components/PersonaDialog.vue';
import MemoryDialog from './components/MemoryDialog.vue';
import HeartbeatDialog from './components/HeartbeatDialog.vue';
import CronDialog from './components/CronDialog.vue';
import LogsDialog from './components/LogsDialog.vue';
import ExtendDialog from './components/ExtendDialog.vue';
import NotificationDialog from './components/NotificationDialog.vue';
// function
import { computed, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { on, off } from '@/composables/mitt';
import { headerTools } from './config';

const { t, locale, setLocale } = useI18n();

/** 全局聊天区背景图：绑定到根容器（铺满整个窗口，含左侧会话列表） */
const {
  backgroundOpacity,
  chatBackgroundStyle,
  chatBackgroundOverlayStyle,
  loadBackground
} = useChatBackground();

/** 语言切换选项：复用系统配置里的语言名（各 locale 中对应自语言名） */
const languageOptions = computed(() => [
  { name: t('config.language.zh'), code: 'zh' },
  { name: t('config.language.en'), code: 'en' },
  { name: t('config.language.ja'), code: 'ja' },
  { name: t('config.language.ko'), code: 'ko' }
]);

/**
 * 语言切换处理器：通过 nuxt-i18n 的 `setLocale` 切换。在 `no_prefix` 策略下
 * `setLocale` 内部的 `navigate()` 会早退，**不会触发路由导航**，因此
 * `/home/:id` 这类会话视图的 URL 保持稳定。
 *
 * `setLocale` 同时完成两件事：
 * - 加载目标 locale 的语言包（`mergeLocaleMessage`），避免渲染原始 key
 * - 写入偏好 cookie（`i18n_redirected`）持久化，刷新后可恢复所选语言
 *
 *（相比直接 `locale.value = code` + 手动写 cookie，`setLocale` 是唯一能保证
 *  语言包被加载的路径，否则首次/切换时 `$t` 返回原始 key。）
 */
async function onLanguageChange(code: string) {
  await setLocale(code as 'zh' | 'en' | 'ja' | 'ko');
  persistLocalePreference(code as 'zh' | 'en' | 'ja' | 'ko');
}

/**
 * 持久化语言偏好 cookie（key: i18n_redirected）。
 *
 * 背景：nuxt.config.ts 设了 `detectBrowserLanguage: false` 后，模块把检测配置归一化为 `{}`，
 * 导致 `setCookieLocale` 因 `detectConfig.useCookie` 为 falsy 而变成空操作——**模块绝不写 cookie**。
 * 因此 `setLocale` 只能即时切换，无法持久化。要满足「浏览器刷新/重开仍是偏好语言」，必须
 * 由我们手动写入偏好 cookie，并在 app.vue 初载时优先读取它（与 app.vue 的读取逻辑配合同套 key）。
 */
function persistLocalePreference(code: 'zh' | 'en' | 'ja' | 'ko') {
  if (import.meta.server) return;
  const pref = useCookie('i18n_redirected');
  pref.value = code;
}
const router = useRouter();
const localePath = useLocalePath();

/**
 * KeepAlive 缓存槽上限（LRU）。
 *
 * 删除**非激活**会话时：服务端 `clearSession` + Dexie 角色快照都会清理，
 * 但该会话的 KeepAlive 缓存槽不会被显式移除（只有删除的是当前激活会话时，
 * 才会因 `router.push('/home')` 离开 `[sid].vue` 路由而随之销毁）。
 * 这些残留槽持续驻留内存，若不设上限只会无界累积。
 * `max` 令 KeepAlive 在缓存槽超过此数时，按 LRU 淘汰最久未访问的会话，
 * 从根本上防止内存增长失控（不影响按 sid 恢复的语义，被淘汰会话下次访问会重建）。
 */
const KEEP_ALIVE_MAX = 20;

/** 技能查看弹窗开关 */
const showSkillsDialog = ref(false);

/** 统计弹窗开关 */
const showStatsDialog = ref(false);

/** 系统配置弹窗开关 */
const showConfigDialog = ref(false);

/** AI人格弹窗开关 */
const showPersonaDialog = ref(false);

/** 记忆弹窗开关 */
const showMemoryDialog = ref(false);

/** 心跳任务弹窗开关 */
const showHeartbeatDialog = ref(false);

/** 定时任务弹窗开关 */
const showCronDialog = ref(false);

/** 日志查看弹窗开关 */
const showLogsDialog = ref(false);

/** 扩展弹窗开关 */
const showExtendDialog = ref(false);

/** 通知查看弹窗开关 */
const showNotificationDialog = ref(false);

/** 通知徽标未读数（由 NotificationDialog 上报） */
const notificationUnread = ref(0);

/** 设置菜单（九宫格）是否展开 */
const isSettingsMenuOpen = ref(false);

/** 左侧历史侧边栏是否折叠（默认展开） */
const isSidebarCollapsed = ref(false);

/** 右侧子 Agent 实时流程图面板是否可见（默认隐藏） */
const isFlowPanelVisible = ref(false);

/** 右侧子 Agent 实时流程图面板是否折叠（默认展开） */
const isFlowCollapsed = ref(false);

/** 右侧子 Agent 实时流程图面板中选中的 run id（用于高亮 + 作为根展示其全部后代） */
const flowSelectedRunId = ref<string | null>(null);

/**
 * 系统配置保存后的回调：当前会话继续保留其已锁定的旧快照 → 显示不变；
 * 仅重读当前会话快照以确认渲染（新会话打开时才取最新全局值）。
 * `ensureSessionCharacter` 由 SessionSidebar.vue 导出复用。
 */
const loadCharacter = async () => {
  if (currentSessionId.value) {
    await ensureSessionCharacter(currentSessionId.value);
  }
};

/** 当前会话 id（用于侧边栏高亮 + NuxtPage 的 KeepAlive key） */
const currentSessionId = ref<string>();

/** 工具触发（仅头部区域；工具栏/图片等已随会话主体迁入 [sid].vue） */
const handleOperate = (type: string, event: string) => {
  if (!event || type !== 'headerBar') return;
  switch (event) {
    case 'skills':
      showSkillsDialog.value = true;
      return;
    case 'knowledgeGraph':
      router.push(localePath('/knowledge-graph'));
      return;
    case 'stats':
      showStatsDialog.value = true;
      return;
    case 'systemConfig':
      showConfigDialog.value = true;
      return;
    case 'persona':
      showPersonaDialog.value = true;
      return;
    case 'memory':
      showMemoryDialog.value = true;
      return;
    case 'heartbeat':
      showHeartbeatDialog.value = true;
      return;
    case 'cron':
      showCronDialog.value = true;
      return;
    case 'logs':
      showLogsDialog.value = true;
      return;
    case 'notification':
      showNotificationDialog.value = true;
      return;
    case 'extend':
      showExtendDialog.value = true;
      return;
    default:
      return;
  }
};

/**
 * 设置菜单（九宫格）项点击处理：先触发对应工具事件，再收起菜单。
 * knowledgeGraph 是路由跳转，其余为弹窗，统一复用 handleOperate 的事件分发。
 */
const handleMenuSelect = (event: string) => {
  isSettingsMenuOpen.value = false;
  handleOperate('headerBar', event);
};

/** 折叠/展开左侧历史侧边栏 */
const toggleSidebar = () => {
  isSidebarCollapsed.value = !isSidebarCollapsed.value;
};

/** 切换右侧子 Agent 实时流程图面板的显示/隐藏 */
const toggleFlowPanel = () => {
  isFlowPanelVisible.value = !isFlowPanelVisible.value;
  // 关闭面板时清空选中，重新打开时从全量树开始
  if (!isFlowPanelVisible.value) {
    flowSelectedRunId.value = null;
  }
};

/** 打开右侧子 Agent 实时流程图面板（由 SessionSidebar 任务项点击触发），并选中该 run */
const openFlowPanel = (runId?: string) => {
  flowSelectedRunId.value = runId ?? null;
  isFlowPanelVisible.value = true;
  isFlowCollapsed.value = false;
};

// 挂载后加载全局聊天区背景图（会话列表拉取已在 SessionSidebar 组件内完成）
onMounted(() => {
  loadBackground();
  // 监听「打开流程图」事件：侧边栏任务项点击时展开右侧面板
  on('subagent:open-flow', openFlowPanel);
});

onBeforeUnmount(() => {
  off('subagent:open-flow', openFlowPanel);
});
</script>
