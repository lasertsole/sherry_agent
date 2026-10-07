<template>
  <div
    class="flex h-full min-h-0 flex-col font-mono text-xs"
    data-test="terminal-panel">
    <!-- Prompt header: where commands run (the session's project directory) and
         the two actions — clear, and re-read the directory after a switch. -->
    <div
      class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-100 px-2 py-1.5 text-[11px] dark:border-gray-800">
      <i class="pi pi-server text-theme-main"></i>
      <span
        class="min-w-0 flex-1 truncate text-gray-500 dark:text-gray-400"
        :title="cwd"
        data-test="terminal-cwd"
        >{{ cwd || t('terminal.noCwd') }}</span
      >
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        :title="t('terminal.refreshCwd')"
        :aria-label="t('terminal.refreshCwd')"
        data-test="terminal-refresh"
        @click="loadInfo" />
      <Button
        icon="pi pi-trash"
        size="small"
        text
        severity="secondary"
        :title="t('terminal.clear')"
        :aria-label="t('terminal.clear')"
        data-test="terminal-clear"
        @click="store.clearTerminal(sessionId)" />
    </div>

    <!-- Scrollback: every run with its command, output and exit code. -->
    <div
      ref="logRef"
      class="min-h-0 flex-1 overflow-auto px-2 py-1.5"
      data-test="terminal-log">
      <p
        v-if="entries.length === 0 && !running"
        class="m-0 py-4 text-center text-[11px] text-gray-400 dark:text-gray-500"
        data-test="terminal-empty">
        {{ t('terminal.empty') }}
      </p>
      <div
        v-for="(entry, index) in entries"
        :key="index"
        class="mb-2"
        :data-test="`terminal-entry-${index}`">
        <div class="flex items-baseline gap-1 text-theme-main">
          <span class="select-none text-gray-400">$</span>
          <span class="whitespace-pre-wrap break-words">{{ entry.command }}</span>
        </div>
        <pre
          v-if="entry.output"
          class="m-0 mt-0.5 whitespace-pre-wrap break-words text-gray-600 dark:text-gray-300"
          >{{ entry.output }}</pre>
        <div class="mt-0.5 text-[10px] text-gray-400 dark:text-gray-500">
          {{ t('terminal.exitCode', { code: entry.exitCode }) }} · {{ entry.durationMs }} ms
          <span v-if="entry.truncated"> · {{ t('terminal.truncated') }}</span>
        </div>
      </div>
      <div
        v-if="running"
        class="flex items-center gap-2 text-[11px] text-gray-400 dark:text-gray-500"
        data-test="terminal-running">
        <ProgressSpinner style="width: 0.9rem; height: 0.9rem" />
        {{ t('terminal.running') }}
      </div>
    </div>

    <!-- The input line. Only the operator types here: the agent's own terminal
         tool is a separate surface with its own approval gate. -->
    <div
      class="flex shrink-0 items-center gap-2 border-t border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800">
      <span class="select-none text-gray-400">$</span>
      <input
        v-model="command"
        type="text"
        class="min-w-0 flex-1 border-0 bg-transparent font-mono text-xs text-theme-main outline-none"
        :placeholder="t('terminal.placeholder')"
        autocomplete="off"
        spellcheck="false"
        data-test="terminal-input"
        @keyup.enter="run" />
      <Button
        icon="pi pi-play"
        size="small"
        outlined
        :loading="running"
        :disabled="!command.trim()"
        :title="t('terminal.run')"
        :aria-label="t('terminal.run')"
        data-test="terminal-run"
        @click="run" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, nextTick, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
// Stores are never auto-imported (unimport only walks app/composables).
import { useToolboxStore } from '~/stores/toolbox';
import type { TerminalRun } from '~/composables/bridge/toolbox';
// Stable module specifier so tests can vi.mock the bridge (the unimport
// injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchTerminalInfo, runTerminalCommand } from '~/composables/bridge/toolbox';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

const route = useRoute();
const sessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));
const store = useToolboxStore();

/** The input line's text. */
const command = ref('');
/** A command is in flight (the input stays usable, the button spins). */
const running = ref(false);
const logRef = ref<HTMLElement | null>(null);

/** The session's scrollback (kept in the store: the panel remounts per tab). */
const entries = computed(() => store.terminalFor(sessionId.value));
/** The directory the prompt shows — refreshed on open and after every run. */
const cwd = computed(() => store.terminalCwd[sessionId.value] ?? '');

/** Read the directory the terminal runs in (fail-open: the prompt stays blank). */
const loadInfo = async (): Promise<void> => {
  if (!sessionId.value) return;
  try {
    const info = await fetchTerminalInfo(sessionId.value);
    store.setTerminalCwd(sessionId.value, info.cwd);
  } catch (e) {
    logUtil.e('[TerminalPanel] Failed to read the terminal cwd:', e);
  }
};

/** Scroll the log to its end (run output arrives at the bottom). */
const scrollToEnd = async (): Promise<void> => {
  await nextTick();
  const el = logRef.value;
  if (el) el.scrollTop = el.scrollHeight;
};

/**
 * Run what the input holds, append the result to the scrollback, and keep the
 * command in the line on a failure so it can be fixed and re-run.
 */
const run = async (): Promise<void> => {
  const line = command.value.trim();
  if (!line || running.value || !sessionId.value) return;
  running.value = true;
  try {
    const result: TerminalRun = await runTerminalCommand(sessionId.value, line);
    store.recordRun(sessionId.value, {
      command: line,
      output: result.output,
      exitCode: result.exit_code,
      durationMs: result.duration_ms,
      truncated: result.truncated,
      cwd: result.cwd
    });
    command.value = '';
  } catch (e) {
    logUtil.e('[TerminalPanel] Failed to run the command:', e);
  } finally {
    running.value = false;
    await scrollToEnd();
  }
};

onMounted(() => {
  void loadInfo();
  void scrollToEnd();
});
</script>

<i18n lang="json">
{
  "zh": {
    "terminal": {
      "placeholder": "输入命令，回车执行",
      "run": "执行",
      "clear": "清空",
      "refreshCwd": "重新读取工作目录",
      "noCwd": "读取工作目录…",
      "empty": "输入命令后回车执行（仅你本人输入，agent 不会用到这个终端）",
      "exitCode": "退出码 {code}",
      "truncated": "输出已截断",
      "running": "执行中…"
    }
  },
  "en": {
    "terminal": {
      "placeholder": "Type a command and press Enter",
      "run": "Run",
      "clear": "Clear",
      "refreshCwd": "Re-read the working directory",
      "noCwd": "reading the working directory…",
      "empty": "Type a command and press Enter (only you type here — the agent never uses this terminal)",
      "exitCode": "exit {code}",
      "truncated": "output truncated",
      "running": "running…"
    }
  },
  "ja": {
    "terminal": {
      "placeholder": "コマンドを入力して Enter",
      "run": "実行",
      "clear": "クリア",
      "refreshCwd": "作業ディレクトリを再取得",
      "noCwd": "作業ディレクトリを取得中…",
      "empty": "コマンドを入力して Enter（入力するのはあなただけ。エージェントはこのターミナルを使いません）",
      "exitCode": "終了コード {code}",
      "truncated": "出力は切り詰められました",
      "running": "実行中…"
    }
  },
  "ko": {
    "terminal": {
      "placeholder": "명령을 입력하고 Enter",
      "run": "실행",
      "clear": "지우기",
      "refreshCwd": "작업 디렉터리 다시 읽기",
      "noCwd": "작업 디렉터리 읽는 중…",
      "empty": "명령을 입력하고 Enter(입력은 사용자만 합니다. 에이전트는 이 터미널을 쓰지 않습니다)",
      "exitCode": "종료 코드 {code}",
      "truncated": "출력이 잘렸습니다",
      "running": "실행 중…"
    }
  }
}
</i18n>
