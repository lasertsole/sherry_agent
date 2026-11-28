<template>
  <div class="flex h-full flex-col gap-4 p-5 text-theme-main">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="m-0 text-lg font-semibold">{{ t('superagent.title') }}</h1>
        <p class="m-0 text-xs text-gray-500">{{ t('superagent.subtitle') }}</p>
      </div>
      <span
        class="rounded px-2 py-1 text-xs"
        :class="error ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700'"
        data-test="superagent-status">
        {{ error || (state?.paused ? t('superagent.paused') : t('superagent.healthy')) }}
      </span>
    </header>

    <nav
      class="flex gap-2 text-sm"
      data-test="superagent-tabs">
      <button
        v-for="tab in tabs"
        :key="tab"
        type="button"
        class="rounded border px-3 py-1"
        :class="active === tab ? 'border-blue-500 bg-blue-50' : 'border-gray-200'"
        :data-test="`superagent-tab-${tab}`"
        @click="active = tab">
        {{ t(`superagent.${tab}`) }}
      </button>
    </nav>

    <section
      class="min-h-0 flex-1 overflow-auto rounded border border-gray-200 p-3"
      data-test="superagent-panel">
      <pre class="m-0 whitespace-pre-wrap break-words text-xs">{{ JSON.stringify(payload, null, 2) }}</pre>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';

type PanelState = {
  needs_attention?: string[];
  problems?: Record<string, unknown>;
  paused?: boolean;
  load?: Record<string, number>;
};

const { t } = useI18n();
const tabs = ['overview', 'source', 'trigger', 'eval', 'execution', 'settings'] as const;
type Tab = (typeof tabs)[number];
const active = ref<Tab>('overview');
const state = ref<PanelState | null>(null);
const settings = ref<Record<string, unknown> | null>(null);
const error = ref('');
let timer: ReturnType<typeof setInterval> | undefined;

const baseUrl = () => {
  const configured = (useRuntimeConfig().public as Record<string, unknown>).superagentUrl;
  return String(configured || 'http://127.0.0.1:3082').replace(/\/$/, '');
};

const request = async (path: string) => {
  const response = await fetch(`${baseUrl()}${path}`, {
    headers: { 'x-orchestrator-request': '1' },
    signal: AbortSignal.timeout(3000)
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
};

const poll = async () => {
  try {
    state.value = (await request('/state')) as PanelState;
    error.value = '';
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
};

const loadTab = async (tab: Tab) => {
  if (tab === 'settings') {
    try {
      settings.value = await request('/settings');
    } catch {
      // The state poll already surfaces the connection error.
    }
  }
};

watch(active, tab => void loadTab(tab));

const payload = computed(() => (active.value === 'settings' ? settings.value : state.value));

onMounted(() => {
  void poll();
  timer = setInterval(() => void poll(), 2000);
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});
</script>
