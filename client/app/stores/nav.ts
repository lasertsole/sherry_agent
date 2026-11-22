/**
 * Data-driven registry for the left sidebar's navigation items (UI upgrade S1).
 *
 * Replaces the hard-coded `ui.sidebarBody: 'sessions' | 'files'` binary with an
 * extensible list. Later plans (S2 experts, S3 task board, S4 meetings) register
 * their own items via `register()` instead of editing this store or the shell.
 */
import { defineStore } from 'pinia';
import { computed, defineAsyncComponent, markRaw, ref, type Component } from 'vue';
import SessionListPanel from '@/pages/home/components/nav/SessionListPanel.vue';
import FilesPanel from '@/pages/home/components/nav/FilesPanel.vue';
import { useTaskflowStore } from './taskflow';
import { useMeetingStore } from './meeting';

export interface NavItem {
  /** Unique id, also the persistence key for the active item */
  id: string;
  /** PrimeIcons class shown in the icon row */
  icon: string;
  /** i18n key for the tooltip/title */
  labelKey: string;
  /** Left-column content component; omitted while a panel is not built yet —
   * the shell then renders the S7 "not implemented" placeholder. */
  component?: Component;
  /** global = not bound to a session; session = bound to the current session */
  scope: 'global' | 'session';
  /** Optional live badge count (e.g. running flows) */
  badge?: () => number;
  /** Minimum left-column width this item needs (default 280) */
  minWidth?: number;
}

/** Build the two built-in items. Async components keep the shell bundle small. */
function builtinItems(): NavItem[] {
  return [
    {
      id: 'chat',
      icon: 'pi-comments',
      labelKey: 'nav.chat',
      // The session list is the default view of the shell: imported
      // synchronously so it is present on the first paint (and testable without
      // a Suspense boundary). Newer, secondary panels stay lazy.
      component: markRaw(SessionListPanel),
      scope: 'global'
    },
    {
      id: 'files',
      icon: 'pi-folder',
      labelKey: 'nav.files',
      component: markRaw(FilesPanel),
      scope: 'session'
    },
    {
      id: 'experts',
      icon: 'pi-users',
      labelKey: 'nav.experts',
      component: markRaw(defineAsyncComponent(() => import('@/pages/home/components/nav/ExpertListPanel.vue'))),
      scope: 'global'
    },
    {
      id: 'tasks',
      icon: 'pi-list-check',
      labelKey: 'nav.tasks',
      component: markRaw(defineAsyncComponent(() => import('@/pages/home/components/nav/TaskBoardNavPanel.vue'))),
      scope: 'global',
      badge: () => useTaskflowStore().runningCount
    },
    {
      id: 'meetings',
      icon: 'pi-video',
      labelKey: 'nav.meetings',
      component: markRaw(defineAsyncComponent(() => import('@/pages/home/components/nav/MeetingListPanel.vue'))),
      scope: 'global',
      badge: () => useMeetingStore().activeCount
    }
  ];
}

export const useNavStore = defineStore(
  'nav',
  () => {
    const items = ref<NavItem[]>(builtinItems());
    const activeId = ref<string>('chat');

    const activeItem = computed(() => items.value.find(item => item.id === activeId.value));

    /**
     * Activate a registered item; unknown ids are ignored.
     * @param id
     */
    function activate(id: string): void {
      if (items.value.some(item => item.id === id)) activeId.value = id;
    }

    /**
     * Register a new item; duplicate ids are ignored so re-registration is idempotent.
     * @param item
     */
    function register(item: NavItem): void {
      if (!items.value.some(existing => existing.id === item.id)) items.value.push(item);
    }

    /**
     * Remove an item, falling back to `chat` if it was active.
     * @param id
     */
    function unregister(id: string): void {
      items.value = items.value.filter(item => item.id !== id);
      if (activeId.value === id) activeId.value = 'chat';
    }

    return { items, activeId, activeItem, activate, register, unregister };
  },
  { persist: { pick: ['activeId'] } }
);
