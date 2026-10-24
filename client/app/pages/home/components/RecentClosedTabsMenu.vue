<template>
  <ToolbarPopover
    ref="popover"
    placement="down"
    align="start"
    class="shrink-0">
    <template #trigger="{ toggle }">
      <button
        type="button"
        class="shrink-0 flex h-6 w-6 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-theme-main dark:text-gray-400 dark:hover:bg-gray-800"
        data-test="recent-closed-toggle"
        :title="t('rightSidebar.recentClosed')"
        :aria-label="t('rightSidebar.recentClosed')"
        @click="toggle">
        <i class="pi pi-chevron-down text-xs"></i>
      </button>
    </template>

    <div
      class="w-60"
      data-test="recent-closed-menu">
      <!-- Two groups, mirroring the strip's 当前会话 / 全局 pills. -->
      <div
        class="flex shrink-0 items-center gap-0.5 rounded-full border border-solid border-gray-200/80 bg-gray-100/80 p-0.5 dark:border-gray-700/80 dark:bg-gray-800/80"
        data-test="recent-closed-scopes">
        <button
          v-for="scope in SCOPES"
          :key="scope"
          type="button"
          class="h-6 flex-1 rounded-full px-2.5 text-xs transition-all duration-300"
          :class="
            scope === recentScope
              ? 'bg-white font-medium text-theme-main shadow-sm dark:bg-[#2a2a36]'
              : 'text-gray-500 hover:text-theme-main dark:text-gray-400'
          "
          :data-test="`recent-closed-scope-${scope}`"
          @click="recentScope = scope">
          {{ t(`rightSidebar.scopes.${scope}`) }}
        </button>
      </div>

      <div
        v-if="entries.length === 0"
        class="px-2 py-3 text-center text-[11px] text-gray-400 dark:text-gray-500"
        data-test="recent-closed-empty">
        {{ t('rightSidebar.recentClosedEmpty') }}
      </div>
      <div
        v-else
        class="mt-1 max-h-64 overflow-y-auto">
        <button
          v-for="entry in entries"
          :key="entry.key"
          type="button"
          class="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
          data-test="recent-closed-entry"
          :data-kind="entry.kind"
          :title="entryLabel(entry)"
          @click="reopen(entry)">
          <i
            :class="entryIcon(entry.kind)"
            class="shrink-0 text-[11px] text-gray-400"></i>
          <span class="truncate">{{ entryLabel(entry) }}</span>
        </button>
      </div>
    </div>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import type { RecentClosedTab, RightSidebarPanelKind, RightSidebarScope } from '~/stores/right-sidebar';

const props = withDefaults(defineProps<{ sessionId?: string }>(), { sessionId: '' });

const { t } = useI18n();
const store = useRightSidebarStore();
const recentScope = ref<RightSidebarScope>('session');
const popover = ref<{ close: () => void } | null>(null);

/** Groups in display order, matching the strip's scope pills. */
const SCOPES: ReadonlyArray<RightSidebarScope> = ['session', 'global'];

/** Icons make a side chat / file / browser entry recognizable at a glance. */
const KIND_ICONS: Partial<Record<RightSidebarPanelKind, string>> = {
  sideChat: 'pi pi-comments',
  fileViewer: 'pi pi-file',
  gitDiff: 'pi pi-code',
  contextViewer: 'pi pi-list',
  sessionPreset: 'pi pi-id-card',
  browser: 'pi pi-globe',
  terminal: 'pi pi-desktop',
  taskDetail: 'pi pi-tasks'
};

/** Closed tabs of the group on screen (current session or global). */
const entries = computed(() => store.recentClosedFor(recentScope.value, props.sessionId));

/**
 * The entry's menu label: the panel name plus the identity the user remembers
 * (a quote preview, a file name or a URL) when the payload carries one.
 * @param entry The recent entry.
 */
function entryLabel(entry: RecentClosedTab): string {
  const base = t(`rightSidebar.${entry.kind}`);
  const payload = entry.payload;
  if (entry.kind === 'sideChat' && payload?.quote) {
    return `${base} · ${preview(payload.quote)}`;
  }
  if (payload?.path) {
    return `${base} · ${payload.path.split('/').pop() ?? payload.path}`;
  }
  if (payload?.url) {
    return `${base} · ${preview(payload.url.replace(/^https?:\/\//, ''))}`;
  }
  return base;
}

/**
 * One-line preview for a quote / URL (the row truncates the rest visually).
 * @param text Source text.
 */
function preview(text: string): string {
  return text.replace(/\s+/g, ' ').trim().slice(0, 28);
}

/**
 * Icon for a panel kind (a neutral glyph for settings editors).
 * @param kind Panel kind.
 */
function entryIcon(kind: RightSidebarPanelKind): string {
  return KIND_ICONS[kind] ?? 'pi pi-window-maximize';
}

/**
 * Restore the closed tab and dismiss the menu.
 * @param entry The entry picked by the user.
 */
function reopen(entry: RecentClosedTab): void {
  store.reopenClosed(entry);
  popover.value?.close();
}
</script>
