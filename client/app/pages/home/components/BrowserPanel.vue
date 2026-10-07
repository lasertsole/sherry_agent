<template>
  <div
    class="flex h-full min-h-0 flex-col"
    data-test="browser-panel">
    <!-- Address bar: back / forward / reload, the URL, and 前往. The panel is a
         window the user can resize (the sidebar's drag handle) or maximize (the
         strip's button), so no extra chrome is invented here. -->
    <div
      class="flex shrink-0 items-center gap-1 border-b border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800">
      <Button
        icon="pi pi-arrow-left"
        size="small"
        text
        severity="secondary"
        :disabled="!canBack"
        :title="t('browser.back')"
        :aria-label="t('browser.back')"
        data-test="browser-back"
        @click="store.step(stateKey, -1)" />
      <Button
        icon="pi pi-arrow-right"
        size="small"
        text
        severity="secondary"
        :disabled="!canForward"
        :title="t('browser.forward')"
        :aria-label="t('browser.forward')"
        data-test="browser-forward"
        @click="store.step(stateKey, 1)" />
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        :disabled="!pageUrl"
        :title="t('browser.reload')"
        :aria-label="t('browser.reload')"
        data-test="browser-reload"
        @click="reload" />
      <InputText
        v-model="address"
        :placeholder="t('browser.placeholder')"
        class="min-w-0 flex-1 font-mono text-xs"
        autocomplete="off"
        spellcheck="false"
        data-test="browser-address"
        @keyup.enter="go" />
      <Button
        icon="pi pi-sign-in"
        size="small"
        outlined
        :title="t('browser.go')"
        :aria-label="t('browser.go')"
        data-test="browser-go"
        @click="go" />
    </div>

    <!-- The page. An iframe is what a web client can offer (the desktop build's
         webview is a separate runtime); sites that refuse framing (X-Frame-Options
         / CSP frame-ancestors) stay blank, which the empty state below names. -->
    <div class="min-h-0 flex-1 bg-white dark:bg-gray-900">
      <iframe
        v-if="pageUrl"
        :key="frameKey"
        :src="pageUrl"
        class="h-full w-full border-0"
        referrerpolicy="no-referrer"
        sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
        data-test="browser-frame" />
      <div
        v-else
        class="flex h-full flex-col items-center justify-center gap-2 px-4 text-center text-xs text-gray-400 dark:text-gray-500"
        data-test="browser-empty">
        <i class="pi pi-desktop text-lg"></i>
        <span>{{ t('browser.empty') }}</span>
        <span class="text-[11px]">{{ t('browser.framingHint') }}</span>
        <div class="mt-1 flex flex-wrap justify-center gap-1.5">
          <button
            v-for="link in QUICK_LINKS"
            :key="link.url"
            type="button"
            class="cursor-pointer rounded border border-solid border-gray-200 px-2 py-0.5 text-[11px] text-gray-500 hover:border-theme-main hover:text-theme-main dark:border-gray-700 dark:text-gray-400"
            @click="quickLink(link.url)">
            {{ link.label }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
// Stores are never auto-imported (unimport only walks app/composables).
import { useToolboxStore } from '~/stores/toolbox';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ payload?: { instance?: string } }>();

const route = useRoute();
const sessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));
/** The instance key the toolbox gave this tab (one per click). */
const instanceKey = computed(() => props.payload?.instance ?? 'browser');
/** THIS panel's identity: the session plus that key — several browsers /
 *  terminals coexist, each with its own history / scrollback. */
const stateKey = computed(() => `${sessionId.value}::${instanceKey.value}`);
const store = useToolboxStore();

/** A few one-click targets for the empty state (the address bar takes any URL). */
const QUICK_LINKS: ReadonlyArray<{ label: string; url: string }> = [
  { label: 'GitHub', url: 'https://github.com' },
  { label: 'MDN', url: 'https://developer.mozilla.org' },
  { label: 'localhost:8080', url: 'http://127.0.0.1:8080' }
];

const pageUrl = computed(() => store.browserFor(stateKey.value).url);
const canBack = computed(() => store.canGoBack(stateKey.value));
const canForward = computed(() => store.canGoForward(stateKey.value));
/** The address bar mirrors the page, but only after a navigation settles — a
 *  half-typed URL must not be overwritten while the user is typing. */
const address = ref(pageUrl.value);
watch(pageUrl, next => {
  address.value = next;
});
/** Bumped on a reload: the iframe re-mounts on a new key (an `src` set to the
 *  same URL is a no-op for the browser). */
const frameKey = ref(0);

/** Navigate to what the address bar holds. */
const go = (): void => {
  store.navigate(stateKey.value, address.value);
};
/** Re-enter the current URL (the panel's 刷新). */
const reload = (): void => {
  frameKey.value += 1;
};
/**
 * Jump to one of the empty state's quick links.
 * @param url
 */
const quickLink = (url: string): void => {
  address.value = url;
  store.navigate(stateKey.value, url);
};
</script>

<i18n lang="json">
{
  "zh": {
    "browser": {
      "placeholder": "输入网址，回车访问",
      "go": "前往",
      "back": "后退",
      "forward": "前进",
      "reload": "刷新",
      "empty": "在上方输入网址开始浏览",
      "framingHint": "有些站点禁止被内嵌（X-Frame-Options / CSP），这类页面会保持空白。"
    }
  },
  "en": {
    "browser": {
      "placeholder": "Type an address and press Enter",
      "go": "Go",
      "back": "Back",
      "forward": "Forward",
      "reload": "Reload",
      "empty": "Type an address above to start browsing",
      "framingHint": "Some sites refuse to be embedded (X-Frame-Options / CSP); those pages stay blank."
    }
  },
  "ja": {
    "browser": {
      "placeholder": "URL を入力して Enter",
      "go": "移動",
      "back": "戻る",
      "forward": "進む",
      "reload": "再読み込み",
      "empty": "上のアドレスバーに URL を入力してください",
      "framingHint": "一部のサイトは埋め込みを拒否します（X-Frame-Options / CSP）。その場合は空白のままです。"
    }
  },
  "ko": {
    "browser": {
      "placeholder": "주소를 입력하고 Enter",
      "go": "이동",
      "back": "뒤로",
      "forward": "앞으로",
      "reload": "새로 고침",
      "empty": "위 주소창에 주소를 입력하세요",
      "framingHint": "일부 사이트는 임베드를 거부합니다(X-Frame-Options / CSP). 그런 페이지는 빈 화면으로 남습니다."
    }
  }
}
</i18n>
