<i18n lang="json">
{
  "en": {
    "logs": {
      "title": "Log Viewer",
      "tabs": {
        "frontend": "Client",
        "backend": "Server"
      },
      "file": "Select log file",
      "bucket": "Select log date",
      "pid": "Select process PID",
      "type": {
        "all": "All",
        "log": "Log",
        "error": "Error"
      },
      "live": "Live",
      "pause": "Pause",
      "refresh": "Refresh",
      "clear": "Clear",
      "autoScroll": "Auto-scroll",
      "connected": "Connected",
      "connecting": "Connecting...",
      "empty": "No logs",
      "liveDisabledHint": "Live view is only available for the currently running server log",
      "currentFile": "Currently running log"
    }
  },
  "ja": {
    "logs": {
      "title": "ログビューア",
      "tabs": {
        "frontend": "クライアント",
        "backend": "サーバー"
      },
      "file": "ログファイルを選択",
      "bucket": "ログ日付を選択",
      "pid": "プロセス PID を選択",
      "type": {
        "all": "すべて",
        "log": "ログ",
        "error": "エラー"
      },
      "live": "リアルタイム",
      "pause": "一時停止",
      "refresh": "更新",
      "clear": "クリア",
      "autoScroll": "自動スクロール",
      "connected": "接続済み",
      "connecting": "接続中...",
      "empty": "ログがありません",
      "liveDisabledHint": "リアルタイム表示は現在実行中のサーバーログのみ利用できます",
      "currentFile": "現在実行中のログ"
    }
  },
  "ko": {
    "logs": {
      "title": "로그 뷰어",
      "tabs": {
        "frontend": "클라이언트",
        "backend": "서버"
      },
      "file": "로그 파일 선택",
      "bucket": "로그 날짜 선택",
      "pid": "프로세스 PID 선택",
      "type": {
        "all": "전체",
        "log": "로그",
        "error": "오류"
      },
      "live": "실시간",
      "pause": "일시정지",
      "refresh": "새로고침",
      "clear": "지우기",
      "autoScroll": "자동 스크롤",
      "connected": "연결됨",
      "connecting": "연결 중...",
      "empty": "로그가 없습니다",
      "liveDisabledHint": "실시간 보기는 현재 실행 중인 서버 로그에서만 사용할 수 있습니다",
      "currentFile": "현재 실행 중인 로그"
    }
  },
  "zh": {
    "logs": {
      "title": "日志查看",
      "tabs": {
        "frontend": "客户端",
        "backend": "服务端"
      },
      "file": "选择日志文件",
      "bucket": "选择日志日期",
      "pid": "选择进程 PID",
      "type": {
        "all": "全部",
        "log": "日志",
        "error": "错误"
      },
      "live": "实时",
      "pause": "暂停",
      "refresh": "刷新",
      "clear": "清空",
      "autoScroll": "自动滚动",
      "connected": "已连接",
      "connecting": "连接中...",
      "empty": "暂无日志",
      "liveDisabledHint": "仅当前运行中的服务端日志支持实时查看",
      "currentFile": "当前运行日志"
    }
  }
}
</i18n>

<template>
  <Dialog
    v-model:visible="visible"
    :header="t('logs.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[1100px]"
    @show="onShow"
    @hide="onHide">
    <TabView v-model:activeIndex="activeTab">
      <!-- ===== 前端日志 Tab ===== -->
      <TabPanel :header="t('logs.tabs.frontend')">
        <div class="flex flex-col gap-3">
          <!-- 工具栏：类型 + 按天分桶下拉（对等 server tab 的「文件 per 天」层级） -->
          <div class="flex items-center gap-2 flex-wrap">
            <Select
              :model-value="selectedType"
              :options="logTypes"
              :placeholder="t('logs.tabs.frontend')"
              class="w-36"
              size="small"
              :loading="loadingTypes"
              @update:model-value="onTypeChange">
              <template #option="slotProps">
                {{ t(`logs.type.${slotProps.option}`) }}
              </template>
            </Select>
            <Select
              :model-value="selectedBucket"
              :options="buckets"
              option-label="name"
              option-value="name"
              :placeholder="t('logs.bucket')"
              class="w-64"
              size="small"
              :loading="loadingBucketContent"
              @update:model-value="onBucketChange">
              <template #option="slotProps">
                <div
                  :class="[
                    'flex items-center justify-between gap-2',
                    slotProps.option.is_current
                      ? 'rounded-md bg-green-100/80 px-1.5 py-0.5 text-green-800 dark:bg-green-500/20 dark:text-green-300'
                      : ''
                  ]">
                  <span :class="slotProps.option.is_current ? 'font-medium' : ''">
                    {{ slotProps.option.name }}
                  </span>
                  <i
                    v-if="slotProps.option.is_current"
                    class="pi pi-play-circle text-green-600 dark:text-green-400"
                    :title="t('logs.currentFile')" />
                </div>
              </template>
            </Select>
            <Button
              :icon="frontendLive ? 'pi pi-pause' : 'pi pi-play'"
              :label="frontendLive ? t('logs.pause') : t('logs.live')"
              :severity="frontendLive ? 'warn' : 'success'"
              size="small"
              :disabled="!selectedBucket || !selectedBucketIsCurrent"
              :title="selectedBucketIsCurrent ? '' : (t('logs.liveDisabledHint') as string)"
              @click="toggleFrontendLive" />
            <Button
              icon="pi pi-refresh"
              :label="t('logs.refresh')"
              size="small"
              :disabled="!selectedBucket"
              @click="loadBucketContent" />
            <Button
              icon="pi pi-trash"
              :label="t('logs.clear')"
              size="small"
              severity="secondary"
              @click="clearFrontend" />
            <div class="flex items-center gap-1.5 ml-auto">
              <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('logs.autoScroll') }}</span>
              <ToggleSwitch v-model="frontendAutoScroll" />
            </div>
          </div>

          <!-- 连接状态提示 -->
          <div v-if="frontendLive" class="flex items-center gap-2 text-xs">
            <i class="pi pi-circle-fill text-green-500" />
            <span class="text-gray-500 dark:text-gray-400">
              {{ t('logs.connected') }}
            </span>
          </div>

          <!-- 前端日志控制台 -->
          <div
            ref="frontendConsoleRef"
            class="overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 font-mono text-xs leading-relaxed"
            style="max-height: 60vh; min-height: 40vh;"
            @scroll="onFrontendScroll">
            <div v-if="loadingBucketContent" class="flex items-center justify-center h-full">
              <ProgressSpinner style="width: 2rem; height: 2rem" />
            </div>
            <template v-else-if="frontendLines.length > 0">
              <div
                v-for="(line, index) in frontendLines"
                :key="index"
                :class="levelClass(line.level)"
                class="whitespace-pre-wrap break-all">
                {{ line.text }}
              </div>
            </template>
            <div v-else class="flex items-center justify-center h-full text-sm text-gray-400">
              {{ t('logs.empty') }}
            </div>
          </div>
        </div>
      </TabPanel>

      <!-- ===== 后端日志 Tab ===== -->
      <TabPanel :header="t('logs.tabs.backend')">
        <div class="flex flex-col gap-3">
          <!-- 工具栏：类型大桶 + 日期列 + PID 列（每行对应一个真实日志文件，日期+PID 定位） -->
          <div class="flex items-center gap-2 flex-wrap">
            <Select
              :model-value="serverSelectedType"
              :options="serverLogTypes"
              :placeholder="t('logs.tabs.backend')"
              class="w-36"
              size="small"
              :loading="loadingFiles"
              @update:model-value="onServerTypeChange">
              <template #option="slotProps">
                {{ t(`logs.type.${slotProps.option}`) }}
              </template>
            </Select>
            <Select
              :model-value="serverSelectedDate"
              :options="serverDates"
              :placeholder="t('logs.bucket')"
              class="w-36"
              size="small"
              :loading="loadingFiles"
              @update:model-value="onServerDateChange" />
            <Select
              :model-value="serverSelectedPid"
              :options="serverPidsForDate"
              option-value="pid"
              option-label="pid"
              :placeholder="t('logs.pid')"
              class="w-28"
              size="small"
              :loading="loadingFiles"
              :disabled="!serverSelectedDate"
              @update:model-value="onServerPidChange">
              <template #option="slotProps">
                <div
                  :class="[
                    'flex items-center justify-between gap-2',
                    slotProps.option.is_current
                      ? 'rounded-md bg-green-100/80 px-1.5 py-0.5 text-green-800 dark:bg-green-500/20 dark:text-green-300'
                      : ''
                  ]">
                  <span :class="slotProps.option.is_current ? 'font-medium' : ''">
                    PID {{ slotProps.option.pid }}
                  </span>
                  <i
                    v-if="slotProps.option.is_current"
                    class="pi pi-play-circle text-green-600 dark:text-green-400"
                    :title="t('logs.currentFile')" />
                </div>
              </template>
            </Select>
            <Button
              :icon="live ? 'pi pi-pause' : 'pi pi-play'"
              :label="live ? t('logs.pause') : t('logs.live')"
              :severity="live ? 'warn' : 'success'"
              size="small"
              :disabled="!serverSelectedBucket || !serverSelectedBucketIsCurrent"
              :title="serverSelectedBucketIsCurrent ? '' : (t('logs.liveDisabledHint') as string)"
              @click="toggleLive" />
            <Button
              icon="pi pi-refresh"
              :label="t('logs.refresh')"
              size="small"
              :disabled="!serverSelectedBucket"
              @click="loadContent" />
            <Button
              icon="pi pi-trash"
              :label="t('logs.clear')"
              size="small"
              severity="secondary"
              @click="clearBackend" />
            <div class="flex items-center gap-1.5 ml-auto">
              <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('logs.autoScroll') }}</span>
              <ToggleSwitch v-model="autoScroll" />
            </div>
          </div>

          <!-- 连接状态提示 -->
          <div v-if="live" class="flex items-center gap-2 text-xs">
            <i
              :class="[
                'pi',
                wsStatus === 'connected' ? 'pi-circle-fill text-green-500' : 'pi-spin pi-spinner text-amber-500'
              ]" />
            <span class="text-gray-500 dark:text-gray-400">
              {{ wsStatus === 'connected' ? t('logs.connected') : t('logs.connecting') }}
            </span>
          </div>

          <!-- 后端日志控制台 -->
          <div
            ref="consoleRef"
            class="overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 font-mono text-xs leading-relaxed"
            style="max-height: 60vh; min-height: 40vh;"
            @scroll="onConsoleScroll">
            <div v-if="loadingContent" class="flex items-center justify-center h-full">
              <ProgressSpinner style="width: 2rem; height: 2rem" />
            </div>
            <template v-else-if="lines.length > 0">
              <div
                v-for="(line, index) in lines"
                :key="index"
                :class="levelClass(line.level)"
                class="whitespace-pre-wrap break-all">
                {{ formatLine(line) }}
              </div>
            </template>
            <div v-else class="flex items-center justify-center h-full text-sm text-gray-400">
              {{ t('logs.empty') }}
            </div>
          </div>
        </div>
      </TabPanel>
    </TabView>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, nextTick, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { listLogFiles, readLogFile, openLogStream } from '@/composables/bridge';
import type { LogFileInfo, LogStreamFrame } from '@/composables/bridge';
import {
  CLIENT_LOG_TYPES,
  installClientLogCapture,
  listClientLogTypes,
  listClientLogBucketsForType,
  readClientLogBucket,
  clearClientLogs,
  subscribeClientLogs,
  levelToType,
  typeOfEntry,
  type ClientLogBucket,
  type ClientLogEntry,
  type ClientLogType,
} from '@/composables/clientLog';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emits('update:modelValue', v),
});

const activeTab = ref(0);

/** 渲染的日志行（含级别，用于着色） */
interface LogLine {
  level: string;
  text: string;
}

/** 渲染行数上限：超出后丢弃最旧的行 */
const MAX_LINES = 5000;

/* ==================== 前端日志（clientLog 组合式：历史 + 实时） ==================== */

// 安装一次浏览器 console 捕获：输出进入内存缓冲（实时）+ IndexedDB（跨重启历史）。
// 捕获为进程级模块单例，幂等；与 server tab 对等提供「历史 + 实时」两段能力。
installClientLogCapture();

const frontendLines = ref<ClientLogEntry[]>([]);
const logTypes = ref<ClientLogType[]>([]); // 固定顺序 all/log/error
const buckets = ref<ClientLogBucket[]>([]); // 当前类型的按天分桶
const selectedType = ref<ClientLogType>('all');
const selectedBucket = ref<string | null>(null);
const loadingTypes = ref(false);
const loadingBucketContent = ref(false);
const frontendLive = ref(false);
const frontendAutoScroll = ref(true);
const frontendConsoleRef = ref<HTMLElement | null>(null);
let frontendUserScrolledUp = false;
let unsubscribeFrontend: (() => void) | null = null;

/** 当前选中的类型分桶（由 selectedType + selectedBucket 解析）。 */
const selectedClientBucket = computed<ClientLogBucket | null>(() => {
  if (!selectedBucket.value) return null;
  return buckets.value.find((x) => x.name === selectedBucket.value) ?? null;
});

/** 当前选中分桶是否为「今天」（只有它可实时推送）。 */
const selectedBucketIsCurrent = computed<boolean>(() => {
  const b = selectedClientBucket.value;
  return !!b?.is_current;
});

/** 实时流是否应接受该条目（仅当前类型；all = 全部）。 */
const entryMatchesSelectedType = (entry: ClientLogEntry): boolean =>
  selectedType.value === 'all' || typeOfEntry(entry) === selectedType.value;

/** 弹窗打开期间实时追加前端新日志（仅当当前选中「今天」分桶且开启实时、类型匹配）。 */
const handleFrontendEntry = (entry: ClientLogEntry) => {
  if (!entryMatchesSelectedType(entry)) return;
  frontendLines.value.push(entry);
  if (frontendLines.value.length > MAX_LINES) frontendLines.value.splice(0, frontendLines.value.length - MAX_LINES);
  scrollFrontendToBottom();
};

/** 加载类型下拉（固定 all/log/error，各带计数），并进入该类型的默认分桶选择。 */
const loadTypeList = async () => {
  loadingTypes.value = true;
  try {
    const infos = await listClientLogTypes();
    logTypes.value = infos.map((i) => i.type);
    await onTypeChange(selectedType.value);
  } catch (e) {
    console.error('[LogsDialog] Failed to load client log types:', e);
    logTypes.value = ['all', 'log', 'error'];
    frontendLines.value = [];
  } finally {
    loadingTypes.value = false;
  }
};

/** 类型切换：重载该类型的分桶列表并默认选中「今天」（今天最新在前）。 */
const loadBucketsForType = async (type: ClientLogType) => {
  loadingBucketContent.value = true;
  try {
    buckets.value = await listClientLogBucketsForType(type);
    if (buckets.value.length > 0) {
      selectedBucket.value = buckets.value[0].name;
      await loadBucketContent();
    } else {
      selectedBucket.value = null;
      frontendLines.value = [];
    }
  } catch (e) {
    console.error('[LogsDialog] Failed to load client log buckets:', e);
    frontendLines.value = [];
  } finally {
    loadingBucketContent.value = false;
  }
};

/** 当前选中分桶的内容（最新在前）。 */
const loadBucketContent = async () => {
  const bucket = selectedClientBucket.value;
  if (!bucket) return;
  loadingBucketContent.value = true;
  try {
    frontendLines.value = await readClientLogBucket(bucket, MAX_LINES);
    scrollFrontendToBottom();
  } catch (e) {
    console.error('[LogsDialog] Failed to read client log bucket:', e);
    frontendLines.value = [];
  } finally {
    loadingBucketContent.value = false;
  }
};

/** 类型切换：停止实时（类型变了）、重载分桶与内容。 */
const onTypeChange = async (type: ClientLogType) => {
  stopFrontendLive();
  selectedType.value = type;
  await loadBucketsForType(type);
};

/** 分桶切换：若离开「今天」则停止实时，否则重新加载内容。 */
const onBucketChange = async (name: string) => {
  stopFrontendLive();
  selectedBucket.value = name;
  await loadBucketContent();
};

/** 切换前端实时开关（仅「今天」分桶可用）。 */
const toggleFrontendLive = () => {
  if (frontendLive.value) {
    stopFrontendLive();
  } else {
    startFrontendLive();
  }
};

/** 启动前端实时流：仅当选中「今天」分桶。 */
const startFrontendLive = () => {
  if (!selectedBucketIsCurrent.value || frontendLive.value) return;
  frontendLive.value = true;
  subscribeFrontend();
};

/** 停止前端实时流。 */
const stopFrontendLive = () => {
  frontendLive.value = false;
  teardownFrontend();
};

/** 清空前端日志：IndexedDB 历史 + 内存缓冲 + 视图 + console 输出。 */
const clearFrontend = async () => {
  frontendLines.value = [];
  try {
    await clearClientLogs();
  } catch (e) {
    console.error('[LogsDialog] Failed to clear client log history:', e);
  }
  if (typeof window !== 'undefined' && window.console && typeof window.console.clear === 'function') {
    window.console.clear();
  }
  // 清空后重载类型/分桶（计数归零）。
  await loadBucketsForType(selectedType.value);
};

/** 开始接收前端实时日志。 */
const subscribeFrontend = () => {
  unsubscribeFrontend?.();
  unsubscribeFrontend = subscribeClientLogs(handleFrontendEntry);
};

/** 停止接收前端实时日志。 */
const teardownFrontend = () => {
  unsubscribeFrontend?.();
  unsubscribeFrontend = null;
};

const scrollFrontendToBottom = async () => {
  if (!frontendAutoScroll.value || frontendUserScrolledUp) return;
  await nextTick();
  const el = frontendConsoleRef.value;
  if (el) el.scrollTop = el.scrollHeight;
};

const onFrontendScroll = () => {
  const el = frontendConsoleRef.value;
  if (!el) return;
  frontendUserScrolledUp = el.scrollHeight - el.scrollTop - el.clientHeight > 40;
};

/* ==================== 后端日志（文件 + WS 实时流） ==================== */

const logFiles = ref<LogFileInfo[]>([]);
const loadingFiles = ref(false);
const loadingContent = ref(false);
const lines = ref<LogLine[]>([]);
const live = ref(false);
const autoScroll = ref(true);
const wsStatus = ref<'idle' | 'connecting' | 'connected'>('idle');

/* ---- server tab 的「类型大桶 → 按天分桶」层级（对等 client tab） ---- */

/** 服务端日志的目录类型 → 统一类型大桶：info → log（INFO 流水）、all → all、error → error。 */
const kindToType = (kind: string): ClientLogType => (kind === 'info' ? 'log' : (kind as ClientLogType));

/** 解析服务端日志文件名 `{kind}_{YYYY-MM-DD}_{pid}.log` 得到类型与日期。 */
const LOG_FILENAME_RE = /^(?<kind>info|all|error)_(?<date>\d{4}-\d{2}-\d{2})_(?<pid>\d+)\.log$/;

/** 类型大桶下拉（固定 all/log/error，对等 client tab）。 */
const serverLogTypes = ref<ClientLogType[]>(CLIENT_LOG_TYPES);
const serverSelectedType = ref<ClientLogType>('all');

/** 服务端日志分桶视图行：一个桶对应后端一个真实的 `.log` 文件（不再按日期合并 PID）。
 *  展示名为日期（YYYY-MM-DD）；同一天存在多个 PID 文件时在展示名追加 PID 以区分。
 *  桶的 `name` 使用文件名（如 `error_2026-08-17_32760.log`）作为唯一键，与 `LogFileInfo` 一一对应。 */
interface ServerLogBucket extends ClientLogBucket {
  /** 后端文件名 `{kind}_{YYYY-MM-DD}_{pid}.log`，即该桶对应的真实日志文件。 */
  file: string;
  /** 该桶对应文件的完整路径（用于读取/实时），消除「同一天多 PID 折叠」歧义。 */
  path: string;
  /** 该桶对应的进程 PID（从文件名解析）。 */
  pid: string;
  /** 该桶日期（YYYY-MM-DD）。 */
  date: string;
}

/** 当前类型的按「日期 + PID」分桶（每个桶对应一个真实文件，最新在前）。数据源，派生日期/PID 两列。 */
const serverBuckets = ref<ServerLogBucket[]>([]);

/** 日期列（第 2 列）：该类型下所有日期，降序。 */
const serverDates = computed<string[]>(() => {
  const set = new Set(serverBuckets.value.map((b) => b.date));
  return [...set].sort((a, b) => b.localeCompare(a));
});

/** PID 列表（第 3 列）：选中日期下的分桶，按 PID 升序。 */
const serverPidsForDate = computed<ServerLogBucket[]>(() =>
  serverBuckets.value
    .filter((b) => b.date === serverSelectedDate.value)
    .sort((a, b) => a.pid.localeCompare(b.pid)),
);

const serverSelectedDate = ref<string | null>(null);
const serverSelectedPid = ref<string | null>(null);

/** 日期列切换：记录所选日期，并默认选中该日期下第一个（PID 升序）PID。 */
const onServerDateChange = async (date: string | null) => {
  stopLive();
  serverSelectedDate.value = date;
  serverSelectedPid.value = null;
  if (date === null) {
    lines.value = [];
    return;
  }
  const pids = serverBuckets.value
    .filter((b) => b.date === date)
    .sort((a, b) => a.pid.localeCompare(b.pid));
  serverSelectedPid.value = pids[0]?.pid ?? null;
  if (pids[0]) await loadContent();
};

/** PID 列切换：更新选中 PID 并读取该 PID 对应的真实文件。 */
const onServerPidChange = async (pid: string | null) => {
  stopLive();
  serverSelectedPid.value = pid;
  if (pid === null) {
    lines.value = [];
    return;
  }
  await loadContent();
};

/** 当前解析出的分桶（由「日期 + PID」定位到具体真实文件），驱动路径与按钮状态。 */
const resolvedServerBucket = computed<ServerLogBucket | null>(() =>
  serverBuckets.value.find(
    (b) => b.date === serverSelectedDate.value && b.pid === serverSelectedPid.value,
  ) ?? null,
);

/** 当前选中分桶的文件名（唯一键），派生自日期 + PID。 */
const serverSelectedBucket = computed<string | null>(() => resolvedServerBucket.value?.name ?? null);

/** 当前选中分桶是否为「今天」（只有它可实时推送）。 */
const serverSelectedBucketIsCurrent = computed<boolean>(() => !!resolvedServerBucket.value?.is_current);

/** 类型切换：重算该类型下分桶，默认选中最新「今天」。 */
const onServerTypeChange = async (type: ClientLogType) => {
  stopLive();
  serverSelectedType.value = type;
  await loadServerBucketsForType(type);
};

/** 按「日期 + PID」分桶：把该类型的每个真实日志文件都作为一个分桶（每个桶一个 PID），
 *  同一天多 PID 不再互相折叠。桶 `name` 用文件名作唯一键，作为「日期 + PID」两列的底层数据源。 */
const buildServerBucketsForType = (type: ClientLogType): ServerLogBucket[] => {
  // 该类型下所有真实文件，按文件名分组计数同日期 PID 数量。
  const files = logFiles.value.filter((f) => {
    const m = f.name.match(LOG_FILENAME_RE);
    return !!m?.groups && kindToType(m.groups['kind']) === type;
  });
  const buckets: ServerLogBucket[] = files.map((f) => {
    const m = f.name.match(LOG_FILENAME_RE)!;
    const date = m.groups!['date'];
    const [y, mo, d] = date.split('-').map(Number);
    const dayStart = new Date(y, mo - 1, d).getTime();
    return {
      type,
      name: f.name, // 唯一键 = 文件名（含 PID），与 LogFileInfo 一一对应
      file: f.name,
      path: f.path,
      pid: m.groups!['pid'],
      date,
      tsStart: dayStart,
      tsEnd: dayStart + 24 * 60 * 60 * 1000,
      count: 1,
      is_current: f.is_current,
    };
  });
  // 停活的当前进程日志优先（今天在前），再按日期降序、PID 升序，保证同日期多 PID 顺序稳定。
  buckets.sort((a, b) => {
    if (a.is_current !== b.is_current) return a.is_current ? -1 : 1;
    if (a.date !== b.date) return b.date.localeCompare(a.date);
    return a.pid.localeCompare(b.pid);
  });
  return buckets;
};

/** 加载该类型的分桶列表：每个后端日志文件一个分桶（含自身路径），
 *  默认选中最新「今天」文件并据此设定「日期 + PID」两列初值。 */
const loadServerBucketsForType = async (type: ClientLogType) => {
  loadingFiles.value = true;
  try {
    const buckets = buildServerBucketsForType(type);
    serverBuckets.value = buckets;
    if (buckets.length > 0) {
      // 默认选中第一个（buildServerBucketsForType 已把「今天」排最前，最新在前）
      serverSelectedDate.value = buckets[0].date;
      serverSelectedPid.value = buckets[0].pid;
      await loadContent();
    } else {
      serverSelectedDate.value = null;
      serverSelectedPid.value = null;
      lines.value = [];
    }
  } catch (e) {
    console.error('[LogsDialog] Failed to build server log buckets:', e);
    lines.value = [];
  } finally {
    loadingFiles.value = false;
  }
};

/** 当前选中分桶的文件路径（用于读取/实时），由「日期 + PID」解析。 */
const serverSelectedFilePath = computed<string | null>(() => resolvedServerBucket.value?.path ?? null);

/** 实时流是否应接受该条日志（仅当前类型；all = 全部）。 */
const frameMatchesSelectedType = (level: string): boolean =>
  serverSelectedType.value === 'all' || levelToType(level) === serverSelectedType.value;

const consoleRef = ref<HTMLElement | null>(null);
let streamHandle: { close: () => void } | null = null;
let userScrolledUp = false;

/** 根据日志级别返回 Tailwind 着色类（dark 模式感知） */
const levelClass = (level: string): string => {
  const lv = (level || '').toUpperCase();
  if (lv === 'TRACE' || lv === 'DEBUG') return 'text-gray-500 dark:text-gray-400';
  if (lv === 'SUCCESS') return 'text-green-600 dark:text-green-400';
  if (lv === 'WARNING') return 'text-amber-600 dark:text-amber-400';
  if (lv === 'ERROR' || lv === 'CRITICAL') return 'text-red-600 dark:text-red-400';
  return 'text-gray-800 dark:text-gray-200';
};

/** 格式化单行日志：`{message}`（原始文本已含时间戳与级别） */
const formatLine = (line: LogLine): string => line.text;

/** 追加日志行（实时流），超出上限时丢弃最旧 */
const appendLines = (newLines: LogLine[]) => {
  if (newLines.length === 0) return;
  lines.value = [...lines.value, ...newLines];
  if (lines.value.length > MAX_LINES) {
    lines.value = lines.value.slice(lines.value.length - MAX_LINES);
  }
  scrollToBottom();
};

/** 自动滚动到底部（仅当开启自动滚动且用户未上翻） */
const scrollToBottom = async () => {
  if (!autoScroll.value || userScrolledUp) return;
  await nextTick();
  const el = consoleRef.value;
  if (el) el.scrollTop = el.scrollHeight;
};

/** 用户滚动时记录是否上翻，用于暂停自动滚动 */
const onConsoleScroll = () => {
  const el = consoleRef.value;
  if (!el) return;
  userScrolledUp = el.scrollHeight - el.scrollTop - el.clientHeight > 40;
};

/** 加载文件列表并默认选中「今天」类型分桶 */
const loadFileList = async () => {
  loadingFiles.value = true;
  try {
    const resp = await listLogFiles();
    logFiles.value = resp.files ?? [];
    await loadServerBucketsForType(serverSelectedType.value);
  } catch (e) {
    console.error('[LogsDialog] Failed to load log files:', e);
    lines.value = [];
  } finally {
    loadingFiles.value = false;
  }
};

/** 读取当前选中分桶（日期文件）的尾部内容 */
const loadContent = async () => {
  const path = serverSelectedFilePath.value;
  if (!path) return;
  loadingContent.value = true;
  try {
    const resp = await readLogFile(path);
    if (resp.success) {
      lines.value = resp.content
        .split('\n')
        .filter((l) => l.trim().length > 0)
        .map((l) => ({ level: inferLevel(l), text: l }));
      scrollToBottom();
    } else {
      lines.value = [];
    }
  } catch (e) {
    console.error('[LogsDialog] Failed to read log file:', e);
    lines.value = [];
  } finally {
    loadingContent.value = false;
  }
};

/** 从原始日志文本行推断级别（用于着色） */
const inferLevel = (text: string): string => {
  const m = text.match(/\b(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\b/);
  return m ? m[1] : 'INFO';
};

/** 切换实时流开关 */
const toggleLive = () => {
  if (live.value) {
    stopLive();
  } else {
    startLive();
  }
};

/** 启动实时日志流：仅当选中「今天」分桶且类型匹配。 */
const startLive = () => {
  if (!serverSelectedBucketIsCurrent.value || live.value) return;
  live.value = true;
  wsStatus.value = 'connecting';
  streamHandle = openLogStream(
    (frame: LogStreamFrame) => {
      if (frame.event === 'ready') {
        wsStatus.value = 'connected';
      } else if (frame.event === 'log' && frame.data) {
        wsStatus.value = 'connected';
        if (frameMatchesSelectedType(frame.data.level)) {
          appendLines([{ level: frame.data.level, text: `${frame.data.timestamp} | ${frame.data.level.padEnd(8, ' ')} | ${frame.data.message}` }]);
        }
      }
    },
    (e) => {
      console.error('[LogsDialog] Log stream error:', e);
      wsStatus.value = 'idle';
    },
  );
};

/** 停止实时日志流 */
const stopLive = () => {
  live.value = false;
  wsStatus.value = 'idle';
  streamHandle?.close();
  streamHandle = null;
};

/** 清空后端当前可见日志 */
const clearBackend = () => {
  lines.value = [];
};

/** 弹窗打开：加载前端分桶历史 + 加载后端文件列表 */
const onShow = () => {
  userScrolledUp = false;
  frontendUserScrolledUp = false;
  loadTypeList();
  loadFileList();
};

/** 弹窗关闭：取消前端实时订阅、停止实时流并重置状态（前端历史保留，便于下次查看） */
const onHide = () => {
  teardownFrontend();
  frontendLive.value = false;
  stopLive();
  lines.value = [];
  logFiles.value = [];
  serverBuckets.value = [];
  serverSelectedDate.value = null;
  serverSelectedPid.value = null;
  serverSelectedType.value = 'all';
  logTypes.value = [];
  buckets.value = [];
  selectedType.value = 'all';
  selectedBucket.value = null;
  userScrolledUp = false;
  frontendUserScrolledUp = false;
};

// 组件卸载时取消前端订阅并清理 WebSocket
onBeforeUnmount(() => {
  teardownFrontend();
  stopLive();
});
</script>
