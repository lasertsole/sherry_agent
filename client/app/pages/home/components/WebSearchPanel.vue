<template>
  <!-- Right-sidebar tab body: mounted and unmounted with its tab, so the mount
       drives every load (backend state + .env provider config). -->
  <div class="flex flex-col gap-3 h-full min-h-0 p-4">
    <div class="flex-1 min-h-0 overflow-y-auto flex flex-col gap-4">
      <!-- ── Section 1: backend choice (per session) ─────────────────────── -->
      <section class="flex flex-col gap-2">
        <div class="flex items-baseline justify-between gap-2">
          <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
            {{ t('webSearch.backend.title') }}
          </span>
          <button
            v-if="state && state.backend"
            type="button"
            class="text-xs text-theme-main hover:underline"
            data-test="web-search-follow-default"
            @click="choose(null)">
            {{ t('webSearch.backend.followDefault') }}
          </button>
        </div>
        <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('webSearch.backend.hint') }}</span>
        <span
          v-if="!sessionId"
          class="text-xs text-amber-600 dark:text-amber-400"
          data-test="web-search-no-session">
          {{ t('webSearch.backend.noSession') }}
        </span>

        <template v-if="noBackendConfigured">
          <div
            class="rounded-lg border border-solid border-amber-300/70 bg-amber-50/60 px-3 py-2 text-xs text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300"
            data-test="web-search-none-configured">
            {{ t('webSearch.backend.none') }}
          </div>
        </template>
        <template v-else>
          <button
            v-for="option in BACKEND_OPTIONS"
            :key="option.id"
            type="button"
            class="flex w-full items-start gap-2 rounded-lg border border-solid px-3 py-2 text-left transition-colors"
            :class="
              selectedBackend === option.id
                ? 'border-theme-main bg-theme-main/5'
                : 'border-gray-light dark:border-[#555]'
            "
            :disabled="!sessionId || !available(option.id)"
            :data-test="`web-search-backend-${option.id}`"
            @click="choose(option.id)">
            <i
              class="pi mt-1 text-xs"
              :class="[
                selectedBackend === option.id ? 'pi-circle-fill' : 'pi-circle',
                available(option.id) ? 'text-theme-main' : 'text-gray-300 dark:text-gray-600'
              ]"></i>
            <span class="min-w-0 flex-1">
              <span class="block text-sm text-theme-main">
                {{ t(`webSearch.backends.${option.id}.name`) }}
              </span>
              <span class="block text-xs text-gray-400 dark:text-gray-500">
                {{ t(`webSearch.backends.${option.id}.desc`) }}
              </span>
              <!-- Unconfigured rows stay visible but inert: the reason (and the
                   fix) is the point of showing them at all. -->
              <span
                v-if="!available(option.id)"
                class="mt-1 block text-[11px] text-amber-600 dark:text-amber-400">
                {{ t(`webSearch.backends.${option.id}.unconfigured`) }}
              </span>
            </span>
          </button>
        </template>

        <div
          v-if="state && !noBackendConfigured"
          class="flex flex-wrap items-center gap-2 text-xs">
          <span class="text-gray-500 dark:text-gray-400">
            {{ t('webSearch.backend.effective', { name: backendLabel(state.effective) }) }}
          </span>
          <span
            v-if="degraded"
            class="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300"
            data-test="web-search-degraded">
            {{ t('webSearch.backend.degraded') }}
          </span>
          <span
            v-if="state.pending"
            class="rounded-full bg-gray-100 px-2 py-0.5 text-gray-500 dark:bg-gray-800 dark:text-gray-400"
            data-test="web-search-pending">
            {{ t('webSearch.backend.pending') }}
          </span>
        </div>
      </section>

      <!-- ── Section 2: SearXNG connection (global, .env) ────────────────── -->
      <section class="flex flex-col gap-2">
        <div class="flex items-baseline justify-between gap-2">
          <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
            {{ t('webSearch.searxng.title') }}
          </span>
          <button
            type="button"
            class="text-xs text-theme-main hover:underline"
            :aria-expanded="searxngOpen"
            data-test="web-search-searxng-toggle"
            @click="searxngOpen = !searxngOpen">
            {{ searxngOpen ? t('webSearch.searxng.collapse') : t('webSearch.searxng.expand') }}
          </button>
        </div>
        <div
          v-if="searxngOpen"
          class="flex flex-col gap-2 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]"
          data-test="web-search-searxng-form">
          <label class="flex flex-col gap-1">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('webSearch.searxng.url') }}</span>
            <InputText
              v-model="form.url"
              placeholder="http://localhost:8888"
              data-test="web-search-searxng-url" />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('webSearch.searxng.proxy') }}</span>
            <InputText
              v-model="form.proxy"
              placeholder="http://127.0.0.1:7890"
              data-test="web-search-searxng-proxy" />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('webSearch.searxng.engines') }}</span>
            <InputText
              v-model="form.engines"
              placeholder="google,bing,duckduckgo"
              data-test="web-search-searxng-engines" />
            <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ t('webSearch.searxng.enginesHint') }}</span>
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('webSearch.searxng.categories') }}</span>
            <Select
              v-model="form.categories"
              :options="CATEGORY_OPTIONS"
              option-label="label"
              option-value="value"
              data-test="web-search-searxng-categories" />
          </label>
          <label class="flex items-center justify-between gap-2">
            <span class="min-w-0">
              <span class="block text-xs text-gray-500 dark:text-gray-400">{{ t('webSearch.searxng.verifyTls') }}</span>
              <span class="block text-[11px] text-gray-400 dark:text-gray-500">
                {{ t('webSearch.searxng.verifyTlsHint') }}
              </span>
            </span>
            <ToggleSwitch
              v-model="form.verifyTls"
              data-test="web-search-searxng-verify-tls" />
          </label>

          <!-- Runtime view (what THIS process loaded at boot) vs the file: a diff
               means the saved value still needs a backend restart to apply. -->
          <div
            v-if="runtimeDiffers"
            class="text-[11px] text-amber-600 dark:text-amber-400"
            data-test="web-search-restart-hint">
            {{ t('webSearch.searxng.restartHint') }}
          </div>

          <div class="flex items-center justify-end gap-2">
            <span
              v-if="saveState === 'saved'"
              class="text-xs text-green-600 dark:text-green-400"
              data-test="web-search-saved">
              {{ t('webSearch.searxng.saved') }}
            </span>
            <span
              v-else-if="saveState === 'failed'"
              class="text-xs text-red-500 dark:text-red-400"
              data-test="web-search-save-failed">
              {{ t('webSearch.searxng.saveFailed') }}
            </span>
            <Button
              :label="t('webSearch.searxng.save')"
              icon="pi pi-save"
              size="small"
              :loading="saving"
              :disabled="loading || saving"
              data-test="web-search-searxng-save"
              @click="saveSearxng" />
          </div>
        </div>
        <span
          v-else
          class="text-xs text-gray-400 dark:text-gray-500">
          {{ t('webSearch.searxng.collapsedHint') }}
        </span>
      </section>

      <!-- ── Section 3: Tavily status (read-only) ────────────────────────── -->
      <section class="flex flex-col gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('webSearch.tavily.title') }}
        </span>
        <div
          class="flex flex-col gap-1 rounded-lg border border-solid border-gray-light px-3 py-2 text-xs dark:border-[#555]"
          data-test="web-search-tavily-status">
          <div class="flex items-center gap-2">
            <i
              class="pi text-xs"
              :class="state?.tavily_available ? 'pi-check-circle text-green-500' : 'pi-times-circle text-gray-400'"></i>
            <span class="text-gray-600 dark:text-gray-300">
              {{ state?.tavily_available ? t('webSearch.tavily.configured') : t('webSearch.tavily.notConfigured') }}
            </span>
          </div>
          <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ t('webSearch.tavily.hint') }}</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import type { SearchBackend } from '~/composables/bridge/session';

const { t } = useI18n({ useScope: 'local' });

/** Which session's backend the panel reads and writes (the open one). */
const route = useRoute();
const sessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));

/** Web-search store: the per-session state and the choice writer. */
const store = useWebSearchStore();

/** The two selectable backends, in display order. */
const BACKEND_OPTIONS: ReadonlyArray<{ id: SearchBackend }> = [{ id: 'tavily' }, { id: 'searxng' }];

/** Search categories SearXNG accepts (empty = its own default). */
const CATEGORY_OPTIONS: Array<{ label: string; value: string }> = [
  { label: '', value: '' },
  { label: 'general', value: 'general' },
  { label: 'it', value: 'it' },
  { label: 'science', value: 'science' },
  { label: 'files', value: 'files' },
  { label: 'images', value: 'images' },
  { label: 'news', value: 'news' }
];

/** `.env` keys the SearXNG form owns. */
const SEARXNG_KEYS = {
  url: 'SEARXNG_URL',
  proxy: 'SEARXNG_PROXY',
  engines: 'SEARXNG_ENGINES',
  categories: 'SEARXNG_CATEGORIES',
  verifyTls: 'SEARXNG_VERIFY_TLS'
} as const;

const loading = ref(true);
const saving = ref(false);
const saveState = ref<'idle' | 'saved' | 'failed'>('idle');
/** The SearXNG card starts collapsed; a configured instance opens it. */
const searxngOpen = ref(false);

/** The editable .env form (file values, i.e. what a save would write). */
const form = ref({ url: '', proxy: '', engines: '', categories: '', verifyTls: true });
/** The same keys as loaded, to send only real changes. */
const original = ref({ ...form.value });

/** The session's backend state (null until the first read lands). */
const state = computed(() => store.stateFor(sessionId.value));

/** The radio selection: the session's explicit choice, falling back to effective. */
const selectedBackend = computed<SearchBackend | null>(() =>
  state.value ? (state.value.backend ?? state.value.effective) : null
);

/** Neither provider is available — the tool answers "unavailable" today. */
const noBackendConfigured = computed(
  () => !!state.value && !state.value.tavily_available && !state.value.searxng.configured
);

/** Chosen Tavily, but the tool serves from SearXNG (Tavily key rejected/expired). */
const degraded = computed(
  () => !!state.value && state.value.backend === 'tavily' && state.value.effective === 'searxng'
);

/**
 * Whether one backend option can be picked (its provider is configured).
 * @param backend
 */
const available = (backend: SearchBackend): boolean =>
  backend === 'tavily' ? state.value?.tavily_available === true : state.value?.searxng.configured === true;

/**
 * Display name of a backend id.
 * @param backend
 */
const backendLabel = (backend: SearchBackend): string => t(`webSearch.backends.${backend}.name`);

/**
 * Whether the saved file values differ from what THIS process loaded at boot —
 * i.e. whether a restart is still owed. Compare the two fields the state
 * payload reports (url / proxy presence); the rest apply on the next boot too,
 * but the URL is the one a user changes most.
 * @returns True when a restart is needed for the draft to take effect.
 */
const runtimeDiffers = computed(() => {
  if (!state.value) return false;
  const runtime = state.value.searxng;
  return (
    form.value.url.trim() !== runtime.url.trim() ||
    form.value.proxy.trim() !== (runtime.proxy ?? '').trim() ||
    form.value.engines.trim() !== (runtime.engines ?? '').trim() ||
    form.value.categories.trim() !== (runtime.categories ?? '').trim() ||
    form.value.verifyTls !== runtime.verify_tls
  );
});

/**
 * Load the SearXNG form from the .env file (the panel's own truth: the file is
 * what the save writes and what the next boot reads).
 */
async function loadSearxngForm(): Promise<void> {
  try {
    const payload = await readEnvConfig();
    const values: Record<string, string> = {};
    for (const group of payload.groups ?? []) {
      for (const entry of group.entries ?? []) values[entry.key] = entry.value;
    }
    form.value = {
      url: values[SEARXNG_KEYS.url] ?? '',
      proxy: values[SEARXNG_KEYS.proxy] ?? '',
      engines: values[SEARXNG_KEYS.engines] ?? '',
      categories: values[SEARXNG_KEYS.categories] ?? '',
      verifyTls: (values[SEARXNG_KEYS.verifyTls] ?? 'true').toLowerCase() !== 'false'
    };
    original.value = { ...form.value };
    // A configured instance is the reason to look at this panel at all — show
    // its form immediately instead of hiding it behind a collapsed section.
    if (form.value.url.trim()) searxngOpen.value = true;
  } catch {
    // A failed read leaves the form empty; the save button stays usable and the
    // backend rejects a key it cannot find in the file.
  } finally {
    loading.value = false;
  }
}

/**
 * Choose a backend for the open session (`null` follows the environment
 * default). A mid-turn choice is parked by the backend — the panel shows it.
 * @param backend
 */
async function choose(backend: SearchBackend | null): Promise<void> {
  const sid = sessionId.value;
  if (!sid) return;
  await store.select(sid, backend);
}

/** Write the SearXNG form back to `.env` (only the keys that changed). */
async function saveSearxng(): Promise<void> {
  saving.value = true;
  saveState.value = 'idle';
  const changes: Record<string, string> = {};
  if (form.value.url.trim() !== original.value.url.trim()) changes[SEARXNG_KEYS.url] = form.value.url.trim();
  if (form.value.proxy.trim() !== original.value.proxy.trim()) changes[SEARXNG_KEYS.proxy] = form.value.proxy.trim();
  if (form.value.engines.trim() !== original.value.engines.trim())
    changes[SEARXNG_KEYS.engines] = form.value.engines.trim();
  if (form.value.categories.trim() !== original.value.categories.trim())
    changes[SEARXNG_KEYS.categories] = form.value.categories.trim();
  if (form.value.verifyTls !== original.value.verifyTls)
    changes[SEARXNG_KEYS.verifyTls] = form.value.verifyTls ? 'true' : 'false';

  if (Object.keys(changes).length === 0) {
    saving.value = false;
    saveState.value = 'saved';
    return;
  }
  const written = await writeEnvConfig(changes);
  saving.value = false;
  if (written) {
    original.value = { ...form.value };
    saveState.value = 'saved';
    // The runtime value only changes at the next boot; the state payload still
    // reports the old one, which is exactly what `runtimeDiffers` surfaces.
    await store.hydrate(sessionId.value);
  } else {
    saveState.value = 'failed';
  }
}

// The panel is mounted with its tab; the mount drives both loads. `readEnvConfig`
// goes through Nuxt's `useFetch(server: true)`, which needs a live setup context —
// a watcher created in setup satisfies that (an event handler would not).
onMounted(() => {
  if (sessionId.value) void store.hydrate(sessionId.value);
  void loadSearxngForm();
});

// Session switches reuse the tab (it lives in the shell, not the page): re-read
// the state for the session now on screen.
watch(sessionId, sid => {
  if (sid) void store.hydrate(sid);
});
</script>

<i18n lang="json">
{
  "zh": {
    "webSearch": {
      "backend": {
        "title": "搜索引擎",
        "hint": "本会话联网搜索（web_search）使用的后端；改动立即生效，运行中的回合会延后到下一轮。",
        "noSession": "未打开会话：切换后端仅在会话内生效（下方连接配置为全局）。",
        "followDefault": "跟随环境默认",
        "none": "未配置任何搜索引擎，web_search 工具不可用。请在下方配置 SearXNG 实例，或在 .env 中填写 TAVILY_API_KEY。",
        "effective": "当前生效：{name}",
        "degraded": "Tavily 降级",
        "pending": "将在下一轮生效"
      },
      "backends": {
        "tavily": {
          "name": "Tavily（推荐）",
          "desc": "付费 API，结果质量高；失效时自动降级到 SearXNG",
          "unconfigured": "未配置（.env 中的 TAVILY_API_KEY 为空）"
        },
        "searxng": {
          "name": "SearXNG（自托管）",
          "desc": "免费开源自托管；不依赖第三方 API key",
          "unconfigured": "未配置（请在下方填写实例地址）"
        }
      },
      "searxng": {
        "title": "SearXNG 连接",
        "expand": "展开",
        "collapse": "收起",
        "collapsedHint": "展开以配置自托管 SearXNG 实例（写入 .env）",
        "url": "实例地址",
        "proxy": "代理",
        "engines": "搜索引擎",
        "enginesHint": "常用：google, bing, duckduckgo, wikipedia, github, stackoverflow。留空使用实例默认引擎集。",
        "categories": "搜索类别",
        "verifyTls": "TLS 校验",
        "verifyTlsHint": "代理使用 MITM HTTPS 证书（Clash / V2Ray）时关闭",
        "restartHint": "修改尚未生效：重启后端后应用（当前进程仍在使用启动时的配置）。",
        "save": "保存",
        "saved": "已保存",
        "saveFailed": "保存失败"
      },
      "tavily": {
        "title": "Tavily 状态",
        "configured": "TAVILY_API_KEY 已配置",
        "notConfigured": "TAVILY_API_KEY 未配置",
        "hint": "密钥保存在 .env（不入库、不随备份外发）；修改后重启后端生效。"
      }
    }
  },
  "en": {
    "webSearch": {
      "backend": {
        "title": "Search engine",
        "hint": "The backend this session's web_search tool uses; a change applies at once, or on the next turn while one is running.",
        "noSession": "No session is open: switching a backend needs one (the connection settings below are global).",
        "followDefault": "Follow the environment default",
        "none": "No search engine is configured, so the web_search tool is unavailable. Configure a SearXNG instance below, or set TAVILY_API_KEY in .env.",
        "effective": "Currently serving from: {name}",
        "degraded": "Tavily downgraded",
        "pending": "Applies on the next turn"
      },
      "backends": {
        "tavily": {
          "name": "Tavily (recommended)",
          "desc": "Paid API, high-quality results; falls back to SearXNG automatically",
          "unconfigured": "Not configured (TAVILY_API_KEY is empty in .env)"
        },
        "searxng": {
          "name": "SearXNG (self-hosted)",
          "desc": "Free and open source, self-hosted; no third-party API key",
          "unconfigured": "Not configured (fill in the instance URL below)"
        }
      },
      "searxng": {
        "title": "SearXNG connection",
        "expand": "Expand",
        "collapse": "Collapse",
        "collapsedHint": "Expand to configure a self-hosted SearXNG instance (written to .env)",
        "url": "Instance URL",
        "proxy": "Proxy",
        "engines": "Engines",
        "enginesHint": "Common: google, bing, duckduckgo, wikipedia, github, stackoverflow. Empty uses the instance's own engine set.",
        "categories": "Category",
        "verifyTls": "TLS verification",
        "verifyTlsHint": "Turn off when the proxy uses a MITM HTTPS certificate (Clash / V2Ray)",
        "restartHint": "Not applied yet: restart the backend to use these values (this process still runs the configuration it started with).",
        "save": "Save",
        "saved": "Saved",
        "saveFailed": "Save failed"
      },
      "tavily": {
        "title": "Tavily status",
        "configured": "TAVILY_API_KEY is configured",
        "notConfigured": "TAVILY_API_KEY is not configured",
        "hint": "The key stays in .env (never stored in a database, never sent with a backup); restart the backend after editing it."
      }
    }
  },
  "ja": {
    "webSearch": {
      "backend": {
        "title": "検索エンジン",
        "hint": "このセッションの web_search が使うバックエンド。変更は即時反映され、実行中のターンでは次ターンに適用されます。",
        "noSession": "セッションを開いていません：バックエンドの切り替えはセッション単位です（下の接続設定はグローバル）。",
        "followDefault": "環境デフォルトに従う",
        "none": "検索エンジンが未設定のため web_search ツールは利用できません。下で SearXNG を設定するか、.env に TAVILY_API_KEY を記入してください。",
        "effective": "現在の実効バックエンド：{name}",
        "degraded": "Tavily から降格",
        "pending": "次ターンで有効"
      },
      "backends": {
        "tavily": {
          "name": "Tavily（推奨）",
          "desc": "有料 API で高品質。失効時は自動で SearXNG に降格",
          "unconfigured": "未設定（.env の TAVILY_API_KEY が空）"
        },
        "searxng": {
          "name": "SearXNG（セルフホスト）",
          "desc": "無料・オープンソースのセルフホスト。サードパーティの API キー不要",
          "unconfigured": "未設定（下にインスタンス URL を入力）"
        }
      },
      "searxng": {
        "title": "SearXNG 接続",
        "expand": "展開",
        "collapse": "折りたたむ",
        "collapsedHint": "展開してセルフホストの SearXNG インスタンスを設定（.env に書き込み）",
        "url": "インスタンス URL",
        "proxy": "プロキシ",
        "engines": "検索エンジン",
        "enginesHint": "よく使うもの：google, bing, duckduckgo, wikipedia, github, stackoverflow。空欄ならインスタンス既定のエンジン群を使用。",
        "categories": "カテゴリ",
        "verifyTls": "TLS 検証",
        "verifyTlsHint": "プロキシが MITM HTTPS 証明書（Clash / V2Ray）を使う場合はオフ",
        "restartHint": "まだ反映されていません：これらの値を使うにはバックエンドを再起動してください（現在のプロセスは起動時の設定のままです）。",
        "save": "保存",
        "saved": "保存しました",
        "saveFailed": "保存に失敗"
      },
      "tavily": {
        "title": "Tavily の状態",
        "configured": "TAVILY_API_KEY は設定済み",
        "notConfigured": "TAVILY_API_KEY は未設定",
        "hint": "キーは .env に保持されます（データベースに保存されず、バックアップにも含まれません）。変更後はバックエンドを再起動してください。"
      }
    }
  },
  "ko": {
    "webSearch": {
      "backend": {
        "title": "검색 엔진",
        "hint": "이 세션의 web_search 도구가 사용하는 백엔드입니다. 변경은 즉시 적용되며, 진행 중인 턴에서는 다음 턴에 적용됩니다.",
        "noSession": "열려 있는 세션이 없습니다: 백엔드 전환은 세션 단위입니다 (아래 연결 설정은 전역).",
        "followDefault": "환경 기본값 따르기",
        "none": "검색 엔진이 설정되지 않아 web_search 도구를 사용할 수 없습니다. 아래에서 SearXNG 인스턴스를 설정하거나 .env에 TAVILY_API_KEY를 입력하세요.",
        "effective": "현재 적용 중: {name}",
        "degraded": "Tavily 강등됨",
        "pending": "다음 턴에 적용"
      },
      "backends": {
        "tavily": {
          "name": "Tavily (권장)",
          "desc": "유료 API로 결과 품질이 높음. 키가 무효하면 자동으로 SearXNG로 강등",
          "unconfigured": "미설정 (.env의 TAVILY_API_KEY가 비어 있음)"
        },
        "searxng": {
          "name": "SearXNG (자체 호스팅)",
          "desc": "무료 오픈소스 자체 호스팅. 서드파티 API 키 불필요",
          "unconfigured": "미설정 (아래에 인스턴스 주소를 입력하세요)"
        }
      },
      "searxng": {
        "title": "SearXNG 연결",
        "expand": "펼치기",
        "collapse": "접기",
        "collapsedHint": "펼쳐서 자체 호스팅 SearXNG 인스턴스를 설정하세요 (.env에 기록)",
        "url": "인스턴스 주소",
        "proxy": "프록시",
        "engines": "검색 엔진",
        "enginesHint": "자주 쓰는 값: google, bing, duckduckgo, wikipedia, github, stackoverflow. 비워 두면 인스턴스 기본 엔진 집합을 사용합니다.",
        "categories": "검색 카테고리",
        "verifyTls": "TLS 검증",
        "verifyTlsHint": "프록시가 MITM HTTPS 인증서(Clash / V2Ray)를 쓸 때 끄세요",
        "restartHint": "아직 적용되지 않았습니다: 이 값을 쓰려면 백엔드를 재시작하세요 (현재 프로세스는 시작 시의 설정을 그대로 사용합니다).",
        "save": "저장",
        "saved": "저장됨",
        "saveFailed": "저장 실패"
      },
      "tavily": {
        "title": "Tavily 상태",
        "configured": "TAVILY_API_KEY 설정됨",
        "notConfigured": "TAVILY_API_KEY 미설정",
        "hint": "키는 .env에 보관됩니다 (데이터베이스에 저장되지 않고 백업에도 포함되지 않습니다). 수정 후 백엔드를 재시작하세요."
      }
    }
  }
}
</i18n>
