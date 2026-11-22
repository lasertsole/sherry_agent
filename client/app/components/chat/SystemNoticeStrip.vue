<template>
  <!-- Transient in-chat system.notice cards: rendered between the transcript
       and the composer dock — they arrive AROUND turns (background command
       exits, external pushes), never persist anywhere, and disappear on
       dismiss / reload. Level drives the color, the source tag names the
       producer, long content folds behind a toggle. -->
  <div
    v-if="visible.length"
    class="flex flex-col gap-2 px-4 pb-2"
    data-test="system-notice-strip">
    <div
      v-for="notice in visible"
      :key="notice.id"
      class="flex flex-col gap-1 rounded-lg border border-solid px-3 py-2 text-xs"
      :class="LEVEL_CLASSES[notice.level]"
      :data-notice-level="notice.level"
      :data-test="`system-notice-${notice.id}`">
      <div class="flex min-w-0 items-center gap-2">
        <i
          class="shrink-0 text-[11px]"
          :class="LEVEL_ICONS[notice.level]"
          aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate font-medium">{{ notice.title }}</span>
        <span
          v-if="notice.source"
          class="shrink-0 rounded-full bg-black/5 px-2 py-0.5 text-[10px] opacity-70"
          data-test="system-notice-source">
          {{ notice.source }}
        </span>
        <button
          type="button"
          class="shrink-0 cursor-pointer opacity-60 hover:opacity-100"
          :aria-label="t('systemNotice.dismiss')"
          :title="t('systemNotice.dismiss')"
          data-test="system-notice-dismiss"
          @click="dismiss(notice.id)">
          <i
            class="pi pi-times text-[10px]"
            aria-hidden="true" />
        </button>
      </div>
      <template v-if="notice.content">
        <p
          class="m-0 whitespace-pre-wrap break-all text-[11px] leading-relaxed opacity-80"
          :class="expandedId === notice.id ? '' : 'line-clamp-4'">
          {{ notice.content }}
        </p>
        <button
          v-if="notice.content.length > FOLD_THRESHOLD"
          type="button"
          class="cursor-pointer self-start text-[11px] underline opacity-60 hover:opacity-100"
          data-test="system-notice-fold"
          @click="expandedId = expandedId === notice.id ? null : notice.id">
          {{ expandedId === notice.id ? t('systemNotice.collapse') : t('systemNotice.expand') }}
        </button>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { off, on } from '@/composables/mitt';
// `useSystemNotices` is a Nuxt auto-import (bare-Vitest mounts get the stub in
// app/composables/__tests__/setup.ts).

/** Content longer than this earns a fold toggle (the card is a peek, not a log). */
const FOLD_THRESHOLD = 240;

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
const { notices, dismiss } = useSystemNotices();

/** Cards of THIS session only (the mitt bus is process-global). */
const visible = computed(() => notices.value.filter(notice => notice.sessionId === props.sessionId));

/** Which card's long content is currently unfolded (null = none). */
const expandedId = ref<number | null>(null);

const LEVEL_CLASSES: Record<string, string> = {
  info: 'border-blue-300/60 bg-blue-50/80 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200',
  success: 'border-emerald-300/60 bg-emerald-50/80 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200',
  warning: 'border-amber-300/60 bg-amber-50/80 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200',
  error: 'border-red-300/60 bg-red-50/80 text-red-800 dark:bg-red-950/50 dark:text-red-200',
  progress: 'border-gray-300/60 bg-gray-50/80 text-gray-700 dark:bg-gray-800/60 dark:text-gray-300'
};

const LEVEL_ICONS: Record<string, string> = {
  info: 'pi pi-info-circle',
  success: 'pi pi-check-circle',
  warning: 'pi pi-exclamation-triangle',
  error: 'pi pi-times-circle',
  progress: 'pi pi-spinner pi-spin'
};

const onNotice = (payload: unknown): void => {
  // Filter BEFORE pushing: every mounted strip shares the process-global list,
  // so a strip must record only its own session's frames (a KeepAlive-cached
  // page's strip never duplicates another session's cards).
  const frame = payload as { session_id?: unknown } | null | undefined;
  if (typeof frame?.session_id !== 'string' || frame.session_id !== props.sessionId) return;
  push(frame as Parameters<typeof push>[0]);
};

const { push, clearSession } = useSystemNotices();

onMounted(() => {
  on('ws:system_notice', onNotice);
});

onBeforeUnmount(() => {
  off('ws:system_notice', onNotice);
  // Notices are per-session memory: a real unmount drops this session's cards
  // (a KeepAlive deactivation keeps them, so switching back still shows them).
  clearSession(props.sessionId);
});
</script>

<i18n lang="json">
{
  "zh": {
    "systemNotice": {
      "dismiss": "关闭这条提示",
      "expand": "展开全部",
      "collapse": "收起"
    }
  },
  "en": {
    "systemNotice": {
      "dismiss": "Dismiss this notice",
      "expand": "Show all",
      "collapse": "Collapse"
    }
  },
  "ja": {
    "systemNotice": {
      "dismiss": "この通知を閉じる",
      "expand": "すべて表示",
      "collapse": "折りたたむ"
    }
  },
  "ko": {
    "systemNotice": {
      "dismiss": "이 알림 닫기",
      "expand": "모두 보기",
      "collapse": "접기"
    }
  }
}
</i18n>
