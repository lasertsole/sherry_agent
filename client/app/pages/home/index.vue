<template>
  <div class="w-full h-full flex text-theme-main">
    <!-- 左侧-历史记录区域 -->
    <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
    <!-- md：固定定位，显示宽度 280px -->
    <!-- lg：相对定位，显示宽度 360px -->
    <div
      :class="[
        'flex flex-col px-4 relative h-full w-[280px] md:w-[280px] lg:w-[360px]',
        'border-r border-solid border-gray-light bg-[#fff] dark:border-gray-dark dark:bg-[#2a2a36]'
      ]">
      <!-- LOGO区域 -->
      <div class="flex items-center h-15 text-xl">🍊{{ t('chatBox.defaultAiName') }}</div>
      <!-- 新建对话 -->
      <Button
        icon="pi pi-comment"
        :label="t('toolbar.newChat')"
        class="mt-3 mb-3"
        @click="handleCreateSession"
        size="small" />
      <!-- 记录列表 -->
      <div class="flex flex-col overflow-auto flex-1 gap-3">
        <div
          v-if="historyList.length === 0"
          class="flex items-center justify-center h-full w-full text-[#868686]">
          {{ t('history.noSessions') }}
        </div>
        <HistoryItem
          v-for="(item, index) in historyList"
          :key="item.id"
          :history-record="item"
          :is-active="currentSessionId === item.id"
          @choose-session="handleToggleSession"
          @delete-session="handleDeleteSession"
          v-model:selectedList="selectedSessionIds" />
      </div>
      <div class="h-17 flex items-center justify-between">
        <div class="flex items-center justify-center gap-1">
          <Checkbox
            :model-value="isCheckAllSession"
            :indeterminate="isIndeterminate"
            binary
            @update:model-value="handleToggleSelectAll" />
          <span>{{ t('history.selectAll') }}</span>
        </div>
        <Button
          icon="pi pi-trash"
          :label="t('history.batchDelete')"
          :disabled="selectedSessionIds.length === 0"
          @click="handleBatchDelete" />
      </div>
    </div>

    <!-- 右侧-会话主体区域 -->
    <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
      <!-- 顶部工具栏 -->
      <div
        class="flex md:justify-end justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- 移动端展示 -->
        <div class="md:hidden h-full flex items-center text-xl">🍊{{ t('chatBox.defaultAiName') }}</div>
        <!-- 顶部工具栏 -->
        <div class="flex items-center gap-3">
          <ModeSwitch />
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
                <span v-if="slotProps.value" class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t(`config.language.${slotProps.value}`) }}</span>
                </span>
                <span v-else class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t('config.language.zh') }}</span>
                </span>
              </template>
              <template #option="slotProps">
                <span>{{ t(`config.language.${slotProps.option.code}`) }}</span>
              </template>
            </Select>
            <Button
              :icon="tool.icon"
              v-for="tool in headerTools"
              :key="tool.event"
              :title="t(tool.title)"
              @click="handleOperate('headerBar', tool.event)"
              :label="t(tool.toolName)"
              variant="text" />
          </div>
        </div>
      </div>
      <!-- 会话主体：每个会话由 [sid].vue 渲染。以 route.params.sid 作为 page-key，
           每个会话获得独立的 KeepAlive 缓存槽，切换时原样恢复其草稿/滚动/流式/HITL 状态。
           `max` 上限：超过 N 个缓存槽时，KeepAlive 会按 LRU 淘汰最久未访问的槽，
           防止删除的非激活会话（其 page-key 不再被路由引用，但槽仍驻留内存）导致无界增长。 -->
      <div class="flex-1 min-h-0">
        <NuxtPage
          :page-key="(route) => String(route.params.sid ?? 'root')"
          :keepalive="{ max: KEEP_ALIVE_MAX }"
        />
      </div>
    </div>

    <!-- 技能查看弹窗 -->
    <SkillsDialog v-model="showSkillsDialog" />

    <!-- 系统配置弹窗 -->
    <ConfigDialog v-model="showConfigDialog" @saved="loadCharacter" />

    <!-- 日志查看弹窗 -->
    <LogsDialog v-model="showLogsDialog" />
  </div>
</template>

<script lang="ts" setup>
// components
import HistoryItem from './components/HistoryItem.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import SkillsDialog from './components/SkillsDialog.vue';
import ConfigDialog from './components/ConfigDialog.vue';
import LogsDialog from './components/LogsDialog.vue';
// function
import { computed, onMounted, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from './type.ts';
import type { CachedCharacter } from '@/composables/db';
import { GLOBAL_SESSION_KEY, DEFAULT_CACHED_CHARACTER, cacheCharacter, readCachedCharacter, clearCachedCharacter, cacheSessionMeta, readCachedSessionMetaList, clearCachedSessionMeta } from '@/composables/db';
import { headerTools } from './config';
import { emit } from '@/composables/mitt';
import { getSessionList, clearSession, SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';

const { t, locale, setLocale } = useI18n();

/** 语言切换选项：复用系统配置里的语言名（各 locale 中对应自语言名） */
const languageOptions = computed(() => [
  { name: t('config.language.zh'), code: 'zh' },
  { name: t('config.language.en'), code: 'en' },
  { name: t('config.language.ja'), code: 'ja' },
  { name: t('config.language.ko'), code: 'ko' },
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
const route = useRoute();
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

/** 系统配置弹窗开关 */
const showConfigDialog = ref(false);

/** 日志查看弹窗开关 */
const showLogsDialog = ref(false);

/**
 * 默认角色显示信息（内置：远野汉娜 / 橘雪莉 + 默认头像 URL，见 `defaultCharacter.ts`）。
 * 用于在会话尚未锁定角色快照时，作为 Dexie 锁定的兜底数据源。
 */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
});

/**
 * 确保指定会话已锁定自己的角色快照。
 *
 * 命名逻辑：系统配置-角色配置编辑的是「全局待定 profile」（`GLOBAL_SESSION_KEY` 行）。
 * 每个会话在首次打开时，把当时的全局 profile 拷贝并锁定到自己的 `session_id` 行；
 * 之后全局更新（改头像/名字）不再作用于已锁定快照的旧会话，仅新会话会取到最新全局值。
 * 锁定结果由 [sid].vue 通过 `readCachedCharacter(sessionId)` 消费。
 *
 * @param sessionId 会话 ID
 */
const ensureSessionCharacter = async (sessionId: string) => {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter(GLOBAL_SESSION_KEY),
      readCachedCharacter(sessionId),
    ]);
    // 会话已有快照（旧会话锁定的头像/名字）→ 保持现状，不覆盖旧会话快照。
    if (sessionSnap) {
      return;
    }
    // 会话尚无快照（新建或从未打开过的会话）→ 用全局 profile 快照并锁定。
    // 注意：`base` 可能是全局行（含 session_id=GLOBAL_SESSION_KEY），
    // 必须用 `...base` 之后显式覆盖 session_id，避免把真实会话的 key 写进全局行。
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
  } catch (error) {
    // Dexie 读写异常时不阻塞聊天。
    console.warn('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
};

/**
 * 系统配置保存后的回调：当前会话继续保留其已锁定的旧快照 → 显示不变；
 * 仅重读当前会话快照以确认渲染（新会话打开时才取最新全局值）。
 */
const loadCharacter = async () => {
  if (currentSessionId.value) {
    await ensureSessionCharacter(currentSessionId.value);
  }
};

/** 历史会话 */
const historyList = ref<SessionRecord[]>([]);

/**
 * 从服务端拉取全部会话列表并填充左侧历史列表。
 *
 * 会话列表的唯一权威来源是服务端（context_engine）。这里会把服务端返回的
 * `{session_id, last_time, title}` 映射为前端 `SessionRecord`（id / createTime / title）。
 * 本地新建但尚未持久化的会话（createTime 为本地时间）会保留在列表头部。
 */
const loadSessionList = async () => {
  try {
    const sessions = await getSessionList();
    // 合并本地新建但服务端尚不存在的会话（IndexedDB 占位，刷新后仍能恢复）：
    // 1) 读取 IndexedDB 中持久化的占位会话（新建空会话尚未发消息）；
    // 2) 服务端已有记录（发过消息）的会话直接从内存列表保留，并顺手清除其占位；
    // 3) 内存 `historyList` 中的本地项（本次会话新建但尚未写入 IndexedDB，兜底）。
    let localPlaceholders = historyList.value.filter(
      (s) => !sessions.some((row) => row.id === s.id),
    );
    const serverIds = new Set(sessions.map((row) => row.id));
    // 服务端已有记录的会话，删除其本地占位（已晋升为真实服务端会话）。
    const placeholders = await readCachedSessionMetaList();
    for (const p of placeholders) {
      if (serverIds.has(p.id)) {
        clearCachedSessionMeta(p.id);
      }
    }
    // 合并：IndexedDB 占位（刷新恢复） + 内存本地项（本次会话兜底），去重。
    const localById = new Map<string, SessionRecord>();
    for (const p of placeholders) {
      localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
    }
    for (const s of localPlaceholders) {
      if (!localById.has(s.id)) localById.set(s.id, s);
    }
    localPlaceholders = Array.from(localById.values());
    // 占位会话按最新优先（createTime 倒序，字符串格式 YYYY-MM-DD HH:mm 可字典序比较）。
    localPlaceholders.sort((a, b) => (b.createTime < a.createTime ? -1 : 1));
    historyList.value = [...localPlaceholders, ...sessions];
  } catch (error) {
    // 服务端不可达时：本次会话内存态保留，并尝试从 IndexedDB 恢复已持久化的占位会话
    console.warn('[loadSessionList] 拉取会话列表失败：', error);
    try {
      const placeholders = await readCachedSessionMetaList();
      const localById = new Map<string, SessionRecord>();
      for (const p of placeholders) {
        localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
      }
      for (const s of historyList.value) {
        if (!localById.has(s.id)) localById.set(s.id, s);
      }
      historyList.value = Array.from(localById.values());
    } catch (cacheErr) {
      console.warn('[loadSessionList] 恢复本地占位会话失败：', cacheErr);
    }
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
    case 'systemConfig':
      showConfigDialog.value = true;
      return;
    case 'logs':
      showLogsDialog.value = true;
      return;
    default:
      return;
  }
};

/** 新增会话：生成随机 session_id，加入列表并路由到新会话页（KeepAlive 按 sid 缓存） */
const handleCreateSession = () => {
  const sessionId = crypto.randomUUID();
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const newSession: SessionRecord = {
    id: sessionId,
    title: t('history.newSession'),
    createTime
  };
  historyList.value = [newSession, ...historyList.value];
  currentSessionId.value = sessionId;
  // 新会话：立即用当前全局 profile 创建并锁定角色快照，保证头像/名字正确显示
  ensureSessionCharacter(sessionId);
  // 持久化占位会话（新建即写入 IndexedDB），保证刷新/重开后该空会话仍保留在列表
  // （服务端会话列表由消息表派生，未发消息前无记录，只能靠本地占位恢复）。
  cacheSessionMeta({ id: sessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() });
  router.push(localePath(`/home/${sessionId}`));
};

/**
 * 会话切换：路由到对应会话页。
 * [sid].vue 由 KeepAlive 按 session_id 缓存，切换时原样恢复其草稿/滚动/流式状态。
 */
const handleToggleSession = (id: string) => {
  if (currentSessionId.value === id) return;
  currentSessionId.value = id;
  // 切换会话：加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
  ensureSessionCharacter(id);
  router.push(localePath(`/home/${id}`));
};

/**
 * 删除会话：调用服务端 clearSession，成功后从列表移除。
 * 若删除的是当前激活会话，则路由回首页空态（[sid].vue 实例由 KeepAlive 释放）。
 */
const handleDeleteSession = async (id: string) => {
  try {
    const ok = await clearSession(id);
    if (!ok) {
      console.warn('[handleDeleteSession] 删除会话失败，保留列表项：', id);
      return;
    }
    historyList.value = historyList.value.filter((s) => s.id !== id);
    selectedSessionIds.value = selectedSessionIds.value.filter((sid) => sid !== id);
    // 同步清理该会话的角色快照缓存
    clearCachedCharacter(id);
    // 同步清理本地占位会话缓存（IndexedDB），避免删除后仍残留占位
    clearCachedSessionMeta(id);
    // 该会话可能仍在流式生成（尤其是非激活会话，其 [sid].vue 仍被 KeepAlive 缓存且流未中止）。
    // 广播中止事件，让对应的 [sid].vue 实例 abort 其 AbortController，避免删除后流仍在后台推块、污染聊天状态。
    emit(SESSION_ABORT_STREAM_EVENT, id);
    if (currentSessionId.value === id) {
      currentSessionId.value = undefined;
      router.push(localePath('/home'));
    }
  } catch (error) {
    console.warn('[handleDeleteSession] 删除会话异常，保留列表项：', id, error);
  }
};

/** 全选状态 */
const isCheckAllSession = ref<boolean>(false);
/** 选择的会话 */
const selectedSessionIds = ref<string[]>([]);
/** 会话选择状态 */
const isIndeterminate = computed(() => {
  if (selectedSessionIds.value.length > 0 && selectedSessionIds.value.length < historyList.value.length) {
    return true;
  } else {
    return false;
  }
});
/** 监听选择 */
watch(
  () => selectedSessionIds.value,
  newVal => {
    if (newVal.length === historyList.value.length) {
      isCheckAllSession.value = true;
    } else {
      isCheckAllSession.value = false;
    }
  }
);

/** 全选/取消全选：在「全选 / 部分选择 / 未选择」三种状态间切换 */
const handleToggleSelectAll = (checked: boolean) => {
  if (checked) {
    // 勾选全选：当前列表全部选中
    selectedSessionIds.value = historyList.value.map((s) => s.id);
  } else {
    // 取消全选：清空当前选择
    selectedSessionIds.value = [];
  }
};

/**
 * 批量删除会话：逐个调用服务端 clearSession，成功后统一从列表移除。
 * 若其中有当前激活会话，则路由回首页空态。
 */
const handleBatchDelete = async () => {
  if (selectedSessionIds.value.length === 0) return;
  if (!window.confirm(t('history.batchDeleteConfirm'))) return;

  const ids = [...selectedSessionIds.value];
  const remain: string[] = [];
  let failed = false;
  for (const id of ids) {
    try {
      const ok = await clearSession(id);
      if (!ok) {
        failed = true;
        remain.push(id);
      }
    } catch (error) {
      failed = true;
      remain.push(id);
      console.warn('[handleBatchDelete] 删除会话异常：', id, error);
    }
  }

  const deleted = ids.filter((id) => !remain.includes(id));
  if (deleted.length > 0) {
    historyList.value = historyList.value.filter((s) => !deleted.includes(s.id));
    // 同步清理被删会话的角色快照缓存
    for (const id of deleted) clearCachedCharacter(id);
    // 被删会话可能仍在流式生成（KeepAlive 缓存内的非激活实例流未中止），
    // 逐个广播中止事件，让对应 [sid].vue 实例 abort 其 AbortController。
    for (const id of deleted) emit(SESSION_ABORT_STREAM_EVENT, id);
  }
  if (currentSessionId.value && deleted.includes(currentSessionId.value)) {
    currentSessionId.value = undefined;
    router.push(localePath('/home'));
  }
  selectedSessionIds.value = remain;

  if (failed && remain.length > 0) {
    console.warn('[handleBatchDelete] 部分会话删除失败，已保留：', remain);
  }
};

// 首屏加载 default 会话的角色显示信息（头像 + 名字）
ensureSessionCharacter('default');
// 挂载后拉取会话列表（角色信息已由 ensureSessionCharacter 从本地 Dexie 加载）
onMounted(() => {
  loadSessionList();
});

// 更强保障：以浏览器 URL 末尾的 session_id 作为激活态的「唯一事实来源」。
// 用 immediate 监听 route.params.sid，同时覆盖三种场景：
//   1) 刷新/直达 /home/{sid}：组件挂载时立即恢复高亮（此前 currentSessionId 初始为 undefined，
//      不恢复则侧边栏无任何激活态背景）；
//   2) 浏览器内导航（后退/前进/改 URL）：sid 变化时同步移动高亮，无需整页刷新；
//   3) 时序竞态：无论 loadSessionList 列表返回先后，只要 URL 带 sid，就始终以它为激活项。
const activeSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});
watch(activeSessionId, async (sid) => {
  currentSessionId.value = sid;
  if (sid) {
    // 加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
    await ensureSessionCharacter(sid);
  }
}, { immediate: true });
</script>
