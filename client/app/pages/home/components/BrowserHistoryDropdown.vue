<template>
  <!-- The address bar's history dropdown: recent/matching visits, newest
       first. Items use `mousedown.prevent` so picking one never blurs (and
       therefore never closes) the input first. -->
  <div
    v-if="open"
    class="absolute left-0 right-0 top-full z-20 mt-1 max-h-72 overflow-y-auto rounded-md border border-solid border-gray-200 bg-white py-1 text-xs shadow-lg dark:border-gray-700 dark:bg-[#20202a]"
    data-test="browser-history-dropdown">
    <button
      v-for="visit in results"
      :key="visit.id ?? visit.url"
      type="button"
      class="flex w-full items-start gap-2 px-2.5 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
      :data-test="`browser-history-item-${visit.id ?? visit.url}`"
      @mousedown.prevent="emit('select', visit.url)">
      <img
        v-if="faviconOf(visit)"
        :src="faviconOf(visit)!"
        alt=""
        class="mt-0.5 h-3.5 w-3.5 shrink-0 rounded-sm"
        @error="onFaviconError($event)" />
      <i
        v-else
        class="pi pi-globe mt-0.5 text-[10px] text-gray-400"></i>
      <span class="min-w-0 flex-1">
        <span class="block truncate font-mono text-gray-700 dark:text-gray-200">{{ visit.url }}</span>
        <span
          v-if="visit.title"
          class="block truncate text-[11px] text-gray-400 dark:text-gray-500">
          {{ visit.title }}
        </span>
        <span class="block text-[10px] text-gray-400 dark:text-gray-500">
          {{ ageText(visit.visitedAt) }}
          <span v-if="visit.sessionId === sessionId"> · {{ t('browser.history.currentSession') }}</span>
        </span>
      </span>
    </button>
    <p
      v-if="!results.length"
      class="m-0 px-2.5 py-1.5 text-gray-400 dark:text-gray-500"
      data-test="browser-history-empty">
      {{ t('browser.history.empty') }}
    </p>
    <button
      v-if="showClear"
      type="button"
      class="mt-1 flex w-full items-center gap-2 border-t border-solid border-gray-100 px-2.5 py-1.5 text-left text-gray-500 hover:bg-gray-100 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800"
      data-test="browser-history-clear"
      @mousedown.prevent="clearAll">
      <i class="pi pi-trash text-[10px]"></i>
      <span>{{ t('browser.history.clear') }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { BrowserVisit } from '~/composables/db';

const props = defineProps<{
  /** What the address bar currently holds (drives the fuzzy match). */
  query: string;
  /** Whether the dropdown is shown (the address bar decides: focus / typing). */
  open: boolean;
  /** The panel's own session (marks rows visited here). */
  sessionId: string;
}>();

const emit = defineEmits<{
  (e: 'select', url: string): void;
}>();

const { t } = useI18n({ useScope: 'local' });

const results = ref<BrowserVisit[]>([]);
/** Favicons that failed to load are hidden for the rest of the session. */
const brokenFavicons = ref<Set<string>>(new Set());

/** The dropdown offers "clear" only when there is something to clear. */
const showClear = ref(false);

/**
 * A visit's favicon URL (the site's own `/favicon.ico`); null once it 404s.
 * @param visit
 */
function faviconOf(visit: BrowserVisit): string | null {
  try {
    const origin = new URL(visit.url).origin;
    if (brokenFavicons.value.has(origin)) return null;
    return `${origin}/favicon.ico`;
  } catch {
    return null;
  }
}

/**
 * A broken favicon hides itself (the Globe glyph takes over).
 * @param event
 */
function onFaviconError(event: Event): void {
  const img = event.target as HTMLImageElement;
  const broken = new Set(brokenFavicons.value);
  try {
    broken.add(new URL(img.src).origin);
  } catch {
    /* ignore */
  }
  brokenFavicons.value = broken;
}

/**
 * The visit's age in the current language (just now / N minutes / N hours /
 * N days).
 * @param visitedAt Epoch ms.
 */
function ageText(visitedAt: number): string {
  const age = visitAge(visitedAt);
  if (age.unit === 'now') return t('browser.history.justNow');
  if (age.unit === 'minutes') return t('browser.history.minutesAgo', { n: age.value });
  if (age.unit === 'hours') return t('browser.history.hoursAgo', { n: age.value });
  return t('browser.history.daysAgo', { n: age.value });
}

/** Empty the history and refresh the list. */
async function clearAll(): Promise<void> {
  await clearBrowserVisits();
  await refresh();
}

/** Re-query the history for the current input. */
async function refresh(): Promise<void> {
  if (!props.open) return;
  results.value = await searchBrowserVisits(props.query);
  showClear.value = props.query.trim().length === 0 && results.value.length > 0;
}

watch(
  () => [props.open, props.query] as const,
  () => void refresh(),
  // Populate immediately: the panel keeps the dropdown mounted and opens it on
  // focus, so the first render must already have something to show.
  { immediate: true }
);
</script>

<i18n lang="json">
{
  "zh": {
    "browser": {
      "history": {
        "justNow": "刚刚",
        "minutesAgo": "{n} 分钟前",
        "hoursAgo": "{n} 小时前",
        "daysAgo": "{n} 天前",
        "currentSession": "当前会话",
        "empty": "暂无历史记录",
        "clear": "清空历史记录"
      }
    }
  },
  "en": {
    "browser": {
      "history": {
        "justNow": "Just now",
        "minutesAgo": "{n} min ago",
        "hoursAgo": "{n} h ago",
        "daysAgo": "{n} d ago",
        "currentSession": "This session",
        "empty": "No history yet",
        "clear": "Clear history"
      }
    }
  },
  "ja": {
    "browser": {
      "history": {
        "justNow": "たった今",
        "minutesAgo": "{n} 分前",
        "hoursAgo": "{n} 時間前",
        "daysAgo": "{n} 日前",
        "currentSession": "このセッション",
        "empty": "履歴はまだありません",
        "clear": "履歴を消去"
      }
    }
  },
  "ko": {
    "browser": {
      "history": {
        "justNow": "방금",
        "minutesAgo": "{n}분 전",
        "hoursAgo": "{n}시간 전",
        "daysAgo": "{n}일 전",
        "currentSession": "현재 세션",
        "empty": "아직 기록이 없습니다",
        "clear": "기록 지우기"
      }
    }
  }
}
</i18n>
