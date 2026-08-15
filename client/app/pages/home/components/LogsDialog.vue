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
          <!-- 工具栏 -->
          <div class="flex items-center gap-2 flex-wrap">
            <Button size="small" severity="secondary" icon="pi pi-trash" :label="t('logs.clear')" @click="clearFrontend" />
            <div class="flex items-center gap-1.5 ml-auto">
              <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('logs.autoScroll') }}</span>
              <ToggleSwitch v-model="frontendAutoScroll" />
            </div>
          </div>
          <!-- 前端日志控制台 -->
          <div
            ref="frontendConsoleRef"
            class="overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 font-mono text-xs leading-relaxed"
            style="max-height: 60vh; min-height: 40vh;"
            @scroll="onFrontendScroll">
            <div
              v-for="(line, index) in frontendLines"
              :key="index"
              :class="levelClass(line.level)"
              class="whitespace-pre-wrap break-all">
              {{ line.text }}
            </div>
            <div v-if="frontendLines.length === 0" class="flex items-center justify-center h-full text-sm text-gray-400">
              {{ t('logs.empty') }}
            </div>
          </div>
        </div>
      </TabPanel>

      <!-- ===== 后端日志 Tab ===== -->
      <TabPanel :header="t('logs.tabs.backend')">
        <div class="flex flex-col gap-3">
          <!-- 工具栏：文件选择 + 操作按钮 -->
          <div class="flex items-center gap-2 flex-wrap">
            <Select
              :model-value="selectedFile"
              :options="logFiles"
              option-label="name"
              option-value="path"
              :placeholder="t('logs.file')"
              class="w-64"
              size="small"
              :loading="loadingFiles"
              @update:model-value="onFileChange">
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
              :icon="live ? 'pi pi-pause' : 'pi pi-play'"
              :label="live ? t('logs.pause') : t('logs.live')"
              :severity="live ? 'warn' : 'success'"
              size="small"
              :disabled="!selectedFile || !selectedFileIsCurrent"
              :title="selectedFileIsCurrent ? '' : (t('logs.liveDisabledHint') as string)"
              @click="toggleLive" />
            <Button
              icon="pi pi-refresh"
              :label="t('logs.refresh')"
              size="small"
              :disabled="!selectedFile"
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

const { t } = useI18n();

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

/* ==================== 前端日志（浏览器 console 捕获） ==================== */

interface ConsoleEntry {
  level: string;
  text: string;
}

/**
 * 全局浏览器 `console.*` 捕获缓冲区。
 * 模块级安装一次（以 `window.__emaConsoleCaptureInstalled` 防重复），
 * 在应用运行期间持续收集前端日志；弹窗关闭也继续捕获，打开后可见最近记录。
 */
const frontendBuffer: ConsoleEntry[] = [];

/** 前端日志实时订阅器集合：弹窗打开期间收到新增日志时实时追加到视图 */
const frontendSubscribers = new Set<(entry: ConsoleEntry) => void>();

function installConsoleCapture(): void {
  if ((window as unknown as Record<string, boolean>).__emaConsoleCaptureInstalled) return;
  (window as unknown as Record<string, boolean>).__emaConsoleCaptureInstalled = true;

  const push = (level: string, args: unknown[]) => {
    let text = '';
    for (const a of args) {
      let s: string;
      try {
        s = typeof a === 'string' ? a : JSON.stringify(a, null, 0);
      } catch {
        s = String(a);
      }
      text = text ? `${text} ${s}` : s;
    }
    if (!text) return;
    const entry: ConsoleEntry = { level, text: `${new Date().toLocaleTimeString()} | ${level.padEnd(8, ' ')} | ${text}` };
    frontendBuffer.push(entry);
    if (frontendBuffer.length > MAX_LINES) frontendBuffer.splice(0, frontendBuffer.length - MAX_LINES);
    // 实时通知处于打开状态的前端日志视图
    frontendSubscribers.forEach((fn) => fn(entry));
  };

  const orig = {
    debug: console.debug,
    log: console.log,
    info: console.info,
    warn: console.warn,
    error: console.error,
  };

  console.debug = (...args: unknown[]) => {
    push('DEBUG', args);
    orig.debug.apply(console, args as never[]);
  };
  console.log = (...args: unknown[]) => {
    push('INFO', args);
    orig.log.apply(console, args as never[]);
  };
  console.info = (...args: unknown[]) => {
    push('INFO', args);
    orig.info.apply(console, args as never[]);
  };
  console.warn = (...args: unknown[]) => {
    push('WARNING', args);
    orig.warn.apply(console, args as never[]);
  };
  console.error = (...args: unknown[]) => {
    push('ERROR', args);
    orig.error.apply(console, args as never[]);
  };
}

installConsoleCapture();

const frontendLines = ref<ConsoleEntry[]>([]);
const frontendAutoScroll = ref(true);
const frontendConsoleRef = ref<HTMLElement | null>(null);
let frontendUserScrolledUp = false;

/** 弹窗打开期间实时追加前端新日志 */
const handleFrontendEntry = (entry: ConsoleEntry) => {
  frontendLines.value.push(entry);
  if (frontendLines.value.length > MAX_LINES) frontendLines.value.splice(0, frontendLines.value.length - MAX_LINES);
  scrollFrontendToBottom();
};

/** 前端缓冲区 ↔ 视图同步（在组件挂载时执行一次） */
const syncFrontend = () => {
  frontendLines.value = [...frontendBuffer];
  scrollFrontendToBottom();
};

/** 开始接收前端实时日志 */
const subscribeFrontend = () => {
  frontendSubscribers.add(handleFrontendEntry);
};

/** 停止接收前端实时日志 */
const unsubscribeFrontend = () => {
  frontendSubscribers.delete(handleFrontendEntry);
};

/** 清空前端捕获缓冲区与视图 */
const clearFrontend = () => {
  frontendBuffer.length = 0;
  frontendLines.value = [];
  if (typeof window !== 'undefined' && window.console && typeof window.console.clear === 'function') {
    window.console.clear();
  }
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
const selectedFile = ref<string | null>(null);
const loadingFiles = ref(false);
const loadingContent = ref(false);
const lines = ref<LogLine[]>([]);
const live = ref(false);
const autoScroll = ref(true);
const wsStatus = ref<'idle' | 'connecting' | 'connected'>('idle');

/** 当前选中文件是否为「后端进程正在写入的日志」（只有它可以实时推送）。 */
const selectedFileIsCurrent = computed<boolean>(() => {
  if (!selectedFile.value) return false;
  const file = logFiles.value.find((f) => f.path === selectedFile.value);
  return !!file?.is_current;
});

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

/** 加载文件列表并默认选中第一个文件 */
const loadFileList = async () => {
  loadingFiles.value = true;
  try {
    const resp = await listLogFiles();
    logFiles.value = resp.files ?? [];
    if (logFiles.value.length > 0) {
      selectedFile.value = logFiles.value[0].path;
      await loadContent();
    } else {
      selectedFile.value = null;
      lines.value = [];
    }
  } catch (e) {
    console.error('[LogsDialog] Failed to load log files:', e);
    lines.value = [];
  } finally {
    loadingFiles.value = false;
  }
};

/** 读取当前选中文件的尾部内容 */
const loadContent = async () => {
  if (!selectedFile.value) return;
  loadingContent.value = true;
  try {
    const resp = await readLogFile(selectedFile.value);
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

/** 文件切换：停止实时流并重新加载内容 */
const onFileChange = async (path: string) => {
  stopLive();
  selectedFile.value = path;
  await loadContent();
};

/** 切换实时流开关 */
const toggleLive = () => {
  if (live.value) {
    stopLive();
  } else {
    startLive();
  }
};

/** 启动实时日志流 */
const startLive = () => {
  if (!selectedFile.value || !selectedFileIsCurrent.value || live.value) return;
  live.value = true;
  wsStatus.value = 'connecting';
  streamHandle = openLogStream(
    (frame: LogStreamFrame) => {
      if (frame.event === 'ready') {
        wsStatus.value = 'connected';
      } else if (frame.event === 'log' && frame.data) {
        wsStatus.value = 'connected';
        appendLines([{ level: frame.data.level, text: `${frame.data.timestamp} | ${frame.data.level.padEnd(8, ' ')} | ${frame.data.message}` }]);
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

/** 弹窗打开：同步前端缓冲区 + 订阅前端实时日志 + 加载后端文件列表 */
const onShow = () => {
  userScrolledUp = false;
  frontendUserScrolledUp = false;
  syncFrontend();
  subscribeFrontend();
  loadFileList();
};

/** 弹窗关闭：取消前端实时订阅、停止实时流并重置后端状态（前端缓冲区保留，便于下次查看） */
const onHide = () => {
  unsubscribeFrontend();
  stopLive();
  lines.value = [];
  logFiles.value = [];
  selectedFile.value = null;
  userScrolledUp = false;
  frontendUserScrolledUp = false;
};

// 组件卸载时取消前端订阅并清理 WebSocket
onBeforeUnmount(() => {
  unsubscribeFrontend();
  stopLive();
});
</script>
