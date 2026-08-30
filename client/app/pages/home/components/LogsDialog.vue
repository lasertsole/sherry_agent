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
      <!-- ===== Frontend logs Tab ===== -->
      <TabPanel
        value="frontend"
        :header="t('logs.tabs.frontend')">
        <div class="flex flex-col gap-3">
          <!-- Toolbar: type + per-day bucket dropdown (mirroring the server tab's "file per day" hierarchy) -->
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

          <!-- Connection status hint -->
          <div
            v-if="frontendLive"
            class="flex items-center gap-2 text-xs">
            <i class="pi pi-circle-fill text-green-500" />
            <span class="text-gray-500 dark:text-gray-400">
              {{ t('logs.connected') }}
            </span>
          </div>

          <!-- Frontend log console -->
          <div
            ref="frontendConsoleRef"
            class="overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 font-mono text-xs leading-relaxed"
            style="max-height: 60vh; min-height: 40vh"
            @scroll="onFrontendScroll">
            <div
              v-if="loadingBucketContent"
              class="flex items-center justify-center h-full">
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
            <div
              v-else
              class="flex items-center justify-center h-full text-sm text-gray-400">
              {{ t('logs.empty') }}
            </div>
          </div>
        </div>
      </TabPanel>

      <!-- ===== Backend logs Tab ===== -->
      <TabPanel
        value="backend"
        :header="t('logs.tabs.backend')">
        <div class="flex flex-col gap-3">
          <!-- Toolbar: type mega-bucket + date column + PID column (each row maps to one real log file, located by date + PID) -->
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
              v-debounce:click.500="toggleLive" />
            <Button
              icon="pi pi-refresh"
              :label="t('logs.refresh')"
              size="small"
              :disabled="!serverSelectedBucket"
              v-debounce:click.300="loadContent" />
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

          <!-- Connection status hint -->
          <div
            v-if="live"
            class="flex items-center gap-2 text-xs">
            <i
              :class="[
                'pi',
                wsStatus === 'connected' ? 'pi-circle-fill text-green-500' : 'pi-spin pi-spinner text-amber-500'
              ]" />
            <span class="text-gray-500 dark:text-gray-400">
              {{ wsStatus === 'connected' ? t('logs.connected') : t('logs.connecting') }}
            </span>
          </div>

          <!-- Backend log console -->
          <div
            ref="consoleRef"
            class="overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 font-mono text-xs leading-relaxed"
            style="max-height: 60vh; min-height: 40vh"
            @scroll="onConsoleScroll">
            <div
              v-if="loadingContent"
              class="flex items-center justify-center h-full">
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
            <div
              v-else
              class="flex items-center justify-center h-full text-sm text-gray-400">
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
import { vDebounce } from '~/directives/debounce';
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
  type ClientLogType
} from '@/composables/clientLog';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const activeTab = ref(0);

/** Rendered log line (includes the level, used for coloring) */
interface LogLine {
  level: string;
  text: string;
}

/** Cap on rendered lines: the oldest lines are dropped once exceeded */
const MAX_LINES = 5000;

/* ==================== Frontend logs (clientLog composable: history + live) ==================== */

// Install the browser console capture once: output goes into an in-memory buffer (live) +
// IndexedDB (history across restarts). The capture is a process-level module singleton and
// idempotent; it provides the same "history + live" two-part capability as the server tab.
installClientLogCapture();

const frontendLines = ref<ClientLogEntry[]>([]);
const logTypes = ref<ClientLogType[]>([]); // fixed order all/log/error
const buckets = ref<ClientLogBucket[]>([]); // per-day buckets for the current type
const selectedType = ref<ClientLogType>('all');
const selectedBucket = ref<string | null>(null);
const loadingTypes = ref(false);
const loadingBucketContent = ref(false);
const frontendLive = ref(false);
const frontendAutoScroll = ref(true);
const frontendConsoleRef = ref<HTMLElement | null>(null);
let frontendUserScrolledUp = false;
let unsubscribeFrontend: (() => void) | null = null;

/** The currently selected type bucket (resolved from selectedType + selectedBucket). */
const selectedClientBucket = computed<ClientLogBucket | null>(() => {
  if (!selectedBucket.value) return null;
  return buckets.value.find(x => x.name === selectedBucket.value) ?? null;
});

/** Whether the selected bucket is "today" (only it can receive live pushes). */
const selectedBucketIsCurrent = computed<boolean>(() => {
  const b = selectedClientBucket.value;
  return !!b?.is_current;
});

/** Whether the live stream should accept this entry (current type only; all = everything). */
const entryMatchesSelectedType = (entry: ClientLogEntry): boolean =>
  selectedType.value === 'all' || typeOfEntry(entry) === selectedType.value;

/** Live-append new frontend logs while the dialog is open (only when the "today" bucket is selected, live is enabled, and the type matches). */
const handleFrontendEntry = (entry: ClientLogEntry) => {
  if (!entryMatchesSelectedType(entry)) return;
  frontendLines.value.push(entry);
  if (frontendLines.value.length > MAX_LINES) frontendLines.value.splice(0, frontendLines.value.length - MAX_LINES);
  scrollFrontendToBottom();
};

/** Load the type dropdown (fixed all/log/error, each with a count) and enter the default bucket selection for that type. */
const loadTypeList = async () => {
  loadingTypes.value = true;
  try {
    const infos = await listClientLogTypes();
    logTypes.value = infos.map(i => i.type);
    await onTypeChange(selectedType.value);
  } catch (e) {
    console.error('[LogsDialog] Failed to load client log types:', e);
    logTypes.value = ['all', 'log', 'error'];
    frontendLines.value = [];
  } finally {
    loadingTypes.value = false;
  }
};

/** Type switch: reload the bucket list for that type and default-select "today" (today, newest first). */
const loadBucketsForType = async (type: ClientLogType) => {
  loadingBucketContent.value = true;
  try {
    buckets.value = await listClientLogBucketsForType(type);
    const firstBucket = buckets.value[0];
    if (firstBucket) {
      selectedBucket.value = firstBucket.name;
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

/** Content of the currently selected bucket (newest first). */
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

/** Type switch: stop live (the type changed) and reload buckets + content. */
const onTypeChange = async (type: ClientLogType) => {
  stopFrontendLive();
  selectedType.value = type;
  await loadBucketsForType(type);
};

/** Bucket switch: stop live when leaving "today"; otherwise reload the content. */
const onBucketChange = async (name: string) => {
  stopFrontendLive();
  selectedBucket.value = name;
  await loadBucketContent();
};

/** Toggle the frontend live switch (only available for the "today" bucket). */
const toggleFrontendLive = () => {
  if (frontendLive.value) {
    stopFrontendLive();
  } else {
    startFrontendLive();
  }
};

/** Start the frontend live stream: only when the "today" bucket is selected. */
const startFrontendLive = () => {
  if (!selectedBucketIsCurrent.value || frontendLive.value) return;
  frontendLive.value = true;
  subscribeFrontend();
};

/** Stop the frontend live stream. */
const stopFrontendLive = () => {
  frontendLive.value = false;
  teardownFrontend();
};

/** Clear frontend logs: IndexedDB history + in-memory buffer + view + console output. */
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
  // Reload types/buckets after clearing (counts reset to zero).
  await loadBucketsForType(selectedType.value);
};

/** Start receiving live frontend logs. */
const subscribeFrontend = () => {
  unsubscribeFrontend?.();
  unsubscribeFrontend = subscribeClientLogs(handleFrontendEntry);
};

/** Stop receiving live frontend logs. */
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

/* ==================== Backend logs (files + WS live stream) ==================== */

const logFiles = ref<LogFileInfo[]>([]);
const loadingFiles = ref(false);
const loadingContent = ref(false);
const lines = ref<LogLine[]>([]);
const live = ref(false);
const autoScroll = ref(true);
const wsStatus = ref<'idle' | 'connecting' | 'connected'>('idle');

/* ---- Server tab's "type mega-bucket → per-day bucket" hierarchy (mirroring the client tab) ---- */

/** Server log directory type → unified type mega-bucket: info → log (INFO stream), all → all, error → error. */
const kindToType = (kind: string): ClientLogType => (kind === 'info' ? 'log' : (kind as ClientLogType));

/** Parse the server log filename `{kind}_{YYYY-MM-DD}_{pid}.log` to get the type and date. */
const LOG_FILENAME_RE = /^(?<kind>info|all|error)_(?<date>\d{4}-\d{2}-\d{2})_(?<pid>\d+)\.log$/;

/** Type mega-bucket dropdown (fixed all/log/error, mirroring the client tab). */
const serverLogTypes = ref<ClientLogType[]>([...CLIENT_LOG_TYPES]);
const serverSelectedType = ref<ClientLogType>('all');

/** Server log bucket view row: each bucket corresponds to one real `.log` file on the backend
 *  (PIDs are no longer merged by date). The display name is the date (YYYY-MM-DD); when multiple
 *  PID files exist on the same day, the PID is appended to the display name to distinguish them.
 *  The bucket's `name` uses the filename (e.g. `error_2026-08-17_32760.log`) as the unique key,
 *  one-to-one with `LogFileInfo`. */
interface ServerLogBucket extends ClientLogBucket {
  /** Backend filename `{kind}_{YYYY-MM-DD}_{pid}.log`, i.e. the real log file this bucket corresponds to. */
  file: string;
  /** Full path of the file this bucket corresponds to (used for reading/live), eliminating the "multiple PIDs folded into the same day" ambiguity. */
  path: string;
  /** Process PID this bucket corresponds to (parsed from the filename). */
  pid: string;
  /** Bucket date (YYYY-MM-DD). */
  date: string;
}

/** Per-"date + PID" buckets for the current type (each bucket maps to one real file, newest first). Source data from which the date/PID columns are derived. */
const serverBuckets = ref<ServerLogBucket[]>([]);

/** Date column (2nd column): all dates under the type, descending. */
const serverDates = computed<string[]>(() => {
  const set = new Set(serverBuckets.value.map(b => b.date));
  return [...set].sort((a, b) => b.localeCompare(a));
});

/** PID list (3rd column): buckets under the selected date, sorted by PID ascending. */
const serverPidsForDate = computed<ServerLogBucket[]>(() =>
  serverBuckets.value.filter(b => b.date === serverSelectedDate.value).sort((a, b) => a.pid.localeCompare(b.pid))
);

const serverSelectedDate = ref<string | null>(null);
const serverSelectedPid = ref<string | null>(null);

/** Date column switch: record the chosen date and default-select the first PID (ascending) under that date. */
const onServerDateChange = async (date: string | null) => {
  stopLive();
  serverSelectedDate.value = date;
  serverSelectedPid.value = null;
  if (date === null) {
    lines.value = [];
    return;
  }
  const pids = serverBuckets.value.filter(b => b.date === date).sort((a, b) => a.pid.localeCompare(b.pid));
  serverSelectedPid.value = pids[0]?.pid ?? null;
  if (pids[0]) await loadContent();
};

/** PID column switch: update the selected PID and read the real file corresponding to that PID. */
const onServerPidChange = async (pid: string | null) => {
  stopLive();
  serverSelectedPid.value = pid;
  if (pid === null) {
    lines.value = [];
    return;
  }
  await loadContent();
};

/** The currently resolved bucket (pinpointed to a concrete real file via "date + PID"), driving the path and button states. */
const resolvedServerBucket = computed<ServerLogBucket | null>(
  () => serverBuckets.value.find(b => b.date === serverSelectedDate.value && b.pid === serverSelectedPid.value) ?? null
);

/** Filename (unique key) of the currently selected bucket, derived from date + PID. */
const serverSelectedBucket = computed<string | null>(() => resolvedServerBucket.value?.name ?? null);

/** Whether the selected bucket is "today" (only it can receive live pushes). */
const serverSelectedBucketIsCurrent = computed<boolean>(() => !!resolvedServerBucket.value?.is_current);

/** Type switch: recompute the buckets under that type, default-selecting the latest "today". */
const onServerTypeChange = async (type: ClientLogType) => {
  stopLive();
  serverSelectedType.value = type;
  await loadServerBucketsForType(type);
};

/** Bucketing by "date + PID": every real log file of the type becomes one bucket (one PID per
 *  bucket), so multiple PIDs on the same day no longer fold into each other. The bucket `name`
 *  uses the filename as the unique key and serves as the underlying data source for the
 *  "date + PID" columns. */
const buildServerBucketsForType = (type: ClientLogType): ServerLogBucket[] => {
  // All real files under this type; filenames are grouped/matched to count the PIDs per date.
  const files = logFiles.value.filter(f => {
    const m = f.name.match(LOG_FILENAME_RE);
    return !!m?.groups && kindToType(m.groups['kind'] ?? '') === type;
  });
  const buckets: ServerLogBucket[] = files.map(f => {
    const m = f.name.match(LOG_FILENAME_RE)!;
    const groups = m.groups!;
    const date = groups['date'] ?? '';
    const pid = groups['pid'] ?? '';
    const [y = 0, mo = 1, d = 1] = date.split('-').map(Number);
    const dayStart = new Date(y, mo - 1, d).getTime();
    return {
      type,
      name: f.name, // unique key = filename (including PID), one-to-one with LogFileInfo
      file: f.name,
      path: f.path,
      pid,
      date,
      tsStart: dayStart,
      tsEnd: dayStart + 24 * 60 * 60 * 1000,
      count: 1,
      is_current: f.is_current
    };
  });
  // Still-alive current-process logs come first (today at the top), then date descending and
  // PID ascending, keeping a stable order for multiple PIDs on the same date.
  buckets.sort((a, b) => {
    if (a.is_current !== b.is_current) return a.is_current ? -1 : 1;
    if (a.date !== b.date) return b.date.localeCompare(a.date);
    return a.pid.localeCompare(b.pid);
  });
  return buckets;
};

/** Load the bucket list for the type: one bucket per backend log file (with its own path),
 *  default-selecting the latest "today" file and using it to set the initial "date + PID"
 *  column values. */
const loadServerBucketsForType = async (type: ClientLogType) => {
  loadingFiles.value = true;
  try {
    const buckets = buildServerBucketsForType(type);
    serverBuckets.value = buckets;
    const firstBucket = buckets[0];
    if (firstBucket) {
      // Default-select the first (buildServerBucketsForType already sorts "today" first, newest first)
      serverSelectedDate.value = firstBucket.date;
      serverSelectedPid.value = firstBucket.pid;
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

/** File path of the currently selected bucket (used for reading/live), resolved from "date + PID". */
const serverSelectedFilePath = computed<string | null>(() => resolvedServerBucket.value?.path ?? null);

/** Whether the live stream should accept this log entry (current type only; all = everything). */
const frameMatchesSelectedType = (level: string): boolean =>
  serverSelectedType.value === 'all' || levelToType(level) === serverSelectedType.value;

const consoleRef = ref<HTMLElement | null>(null);
let streamHandle: { close: () => void } | null = null;
let userScrolledUp = false;

/** Return the Tailwind color classes for a log level (dark-mode aware) */
const levelClass = (level: string): string => {
  const lv = (level || '').toUpperCase();
  if (lv === 'TRACE' || lv === 'DEBUG') return 'text-gray-500 dark:text-gray-400';
  if (lv === 'SUCCESS') return 'text-green-600 dark:text-green-400';
  if (lv === 'WARNING') return 'text-amber-600 dark:text-amber-400';
  if (lv === 'ERROR' || lv === 'CRITICAL') return 'text-red-600 dark:text-red-400';
  return 'text-gray-800 dark:text-gray-200';
};

/** Format a single log line: `{message}` (the raw text already contains the timestamp and level) */
const formatLine = (line: LogLine): string => line.text;

/** Append log lines (live stream), dropping the oldest when over the cap */
const appendLines = (newLines: LogLine[]) => {
  if (newLines.length === 0) return;
  lines.value = [...lines.value, ...newLines];
  if (lines.value.length > MAX_LINES) {
    lines.value = lines.value.slice(lines.value.length - MAX_LINES);
  }
  scrollToBottom();
};

/** Auto-scroll to the bottom (only when auto-scroll is on and the user has not scrolled up) */
const scrollToBottom = async () => {
  if (!autoScroll.value || userScrolledUp) return;
  await nextTick();
  const el = consoleRef.value;
  if (el) el.scrollTop = el.scrollHeight;
};

/** Record whether the user scrolled up, used to pause auto-scrolling */
const onConsoleScroll = () => {
  const el = consoleRef.value;
  if (!el) return;
  userScrolledUp = el.scrollHeight - el.scrollTop - el.clientHeight > 40;
};

/** Load the file list and default-select the "today" type bucket */
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

/** Read the tail content of the currently selected bucket (date file) */
const loadContent = async () => {
  const path = serverSelectedFilePath.value;
  if (!path) return;
  loadingContent.value = true;
  try {
    const resp = await readLogFile(path);
    if (resp.success) {
      lines.value = resp.content
        .split('\n')
        .filter(l => l.trim().length > 0)
        .map(l => ({ level: inferLevel(l), text: l }));
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

/** Infer the level from a raw log text line (used for coloring) */
const inferLevel = (text: string): string => {
  const m = text.match(/\b(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\b/);
  return m?.[1] ?? 'INFO';
};

/** Toggle the live stream switch */
const toggleLive = () => {
  if (live.value) {
    stopLive();
  } else {
    startLive();
  }
};

/** Start the live log stream: only when the "today" bucket is selected and the type matches. */
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
          appendLines([
            {
              level: frame.data.level,
              text: `${frame.data.timestamp} | ${frame.data.level.padEnd(8, ' ')} | ${frame.data.message}`
            }
          ]);
        }
      }
    },
    e => {
      console.error('[LogsDialog] Log stream error:', e);
      wsStatus.value = 'idle';
    }
  );
};

/** Stop the live log stream */
const stopLive = () => {
  live.value = false;
  wsStatus.value = 'idle';
  streamHandle?.close();
  streamHandle = null;
};

/** Clear the currently visible backend logs */
const clearBackend = () => {
  lines.value = [];
};

/** Dialog opened: load the frontend bucket history + the backend file list */
const onShow = () => {
  userScrolledUp = false;
  frontendUserScrolledUp = false;
  loadTypeList();
  loadFileList();
};

/** Dialog closed: cancel the frontend live subscription, stop the live stream, and reset state (frontend history is kept for the next viewing) */
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

// Cancel the frontend subscription and clean up the WebSocket when the component unmounts
onBeforeUnmount(() => {
  teardownFrontend();
  stopLive();
});
</script>
