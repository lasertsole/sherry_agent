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
      <!-- Free size (ZCode's responsive mode): the page renders in a fixed
           emulated frame the user resizes with the handles around it. -->
      <Button
        :icon="responsive ? 'pi pi-expand' : 'pi pi-tablet'"
        size="small"
        text
        :severity="responsive ? 'primary' : 'secondary'"
        :aria-pressed="responsive"
        :title="responsive ? t('browser.freeSizeOff') : t('browser.freeSizeOn')"
        :aria-label="responsive ? t('browser.freeSizeOff') : t('browser.freeSizeOn')"
        data-test="browser-free-size"
        @click="toggleResponsive" />
      <!-- DevTools: a cross-origin iframe cannot hand out its devtools, so this
           opens the page in a real window where the browser's own devtools work
           (ZCode's button is an Electron <webview>.openDevTools()). -->
      <Button
        icon="pi pi-external-link"
        size="small"
        text
        severity="secondary"
        :disabled="!pageUrl"
        :title="t('browser.devtoolsHint')"
        :aria-label="t('browser.devtools')"
        data-test="browser-devtools"
        @click="openExternal" />
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
    <div
      ref="canvasRef"
      class="min-h-0 flex-1 bg-white dark:bg-gray-900"
      :class="responsive ? 'overflow-auto bg-gray-100 p-4 dark:bg-gray-950' : ''">
      <!-- Free-size canvas: the frame is laid out at the emulated size and scaled
           down to fit the panel (ZCode's `fit` zoom), centred like a device. -->
      <div
        v-if="responsive && pageUrl"
        class="flex min-h-full w-max min-w-full items-center justify-center"
        data-test="browser-free-canvas">
        <div
          class="relative bg-white shadow-sm ring-1 ring-gray-200 dark:ring-gray-700"
          :style="{ width: `${viewport.width * frameScale}px`, height: `${viewport.height * frameScale}px` }"
          data-test="browser-free-frame">
          <div
            class="origin-top-left bg-white"
            :style="{
              width: `${viewport.width}px`,
              height: `${viewport.height}px`,
              transform: `scale(${frameScale})`
            }"
            :data-responsive-width="viewport.width"
            :data-responsive-height="viewport.height">
            <iframe
              :key="`${frameKey}-${responsive}`"
              :src="pageUrl"
              class="h-full w-full border-0"
              referrerpolicy="no-referrer"
              sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
              data-test="browser-frame" />
          </div>
          <!-- Eight handles, ZCode's own set: four edges (a grip appears on
               hover) and four corners; pointer capture + keyboard arrows. -->
          <div
            v-for="handle in RESIZE_HANDLES"
            :key="handle.key"
            class="group/handle absolute z-30 touch-none outline-none"
            :class="handle.className"
            :style="handle.style"
            role="separator"
            :tabindex="0"
            :aria-label="t('browser.resizeHandle')"
            :data-test="`browser-resize-${handle.key}`"
            @pointerdown="beginResize(handle, $event)"
            @pointermove="onResizeMove($event)"
            @pointerup="endResize($event)"
            @pointercancel="endResize($event)"
            @lostpointercapture="endResize($event)"
            @keydown="onResizeKey(handle, $event)">
            <i
              v-if="handle.grip"
              class="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 text-[10px] text-gray-300 opacity-0 transition-opacity group-hover/handle:opacity-100 dark:text-gray-600"
              :class="handle.grip" />
          </div>
        </div>
      </div>
      <iframe
        v-else-if="pageUrl"
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

/** Free-size mode and the emulated frame's size (per instance, in the store). */
const responsive = computed(() => store.browserFor(stateKey.value).responsive);
const viewport = computed(() => store.browserFor(stateKey.value).viewport);

/**
 * The eight resize handles, mirroring ZCode's own set: four edges (a grip that
 * fades in on hover) and four corners, each carrying the direction it moves the
 * width / height by.
 */
const RESIZE_HANDLES: ReadonlyArray<{
  key: string;
  className: string;
  style?: Record<string, string>;
  grip: string;
  widthDirection: -1 | 0 | 1;
  heightDirection: -1 | 0 | 1;
}> = [
  {
    key: 'left',
    className: 'top-0 cursor-ew-resize',
    style: { left: '-6px', width: '6px', height: '100%' },
    grip: 'pi pi-bars rotate-90',
    widthDirection: -1,
    heightDirection: 0
  },
  {
    key: 'right',
    className: 'top-0 cursor-ew-resize',
    style: { right: '-6px', width: '6px', height: '100%' },
    grip: 'pi pi-bars rotate-90',
    widthDirection: 1,
    heightDirection: 0
  },
  {
    key: 'top',
    className: 'left-0 cursor-ns-resize',
    style: { top: '-6px', height: '6px', width: '100%' },
    grip: 'pi pi-bars',
    widthDirection: 0,
    heightDirection: -1
  },
  {
    key: 'bottom',
    className: 'left-0 cursor-ns-resize',
    style: { bottom: '-6px', height: '6px', width: '100%' },
    grip: 'pi pi-bars',
    widthDirection: 0,
    heightDirection: 1
  },
  {
    key: 'top-left',
    className: 'cursor-nwse-resize',
    style: { top: '-6px', left: '-6px', width: '12px', height: '12px' },
    grip: '',
    widthDirection: -1,
    heightDirection: -1
  },
  {
    key: 'top-right',
    className: 'cursor-nesw-resize',
    style: { top: '-6px', right: '-6px', width: '12px', height: '12px' },
    grip: '',
    widthDirection: 1,
    heightDirection: -1
  },
  {
    key: 'bottom-left',
    className: 'cursor-nesw-resize',
    style: { bottom: '-6px', left: '-6px', width: '12px', height: '12px' },
    grip: '',
    widthDirection: -1,
    heightDirection: 1
  },
  {
    key: 'bottom-right',
    className: 'cursor-nwse-resize',
    style: { bottom: '-6px', right: '-6px', width: '12px', height: '12px' },
    grip: '',
    widthDirection: 1,
    heightDirection: 1
  }
];

/** The canvas under the frame (the free-size mode's scroll viewport). */
const canvasRef = ref<HTMLElement | null>(null);

/**
 * Scale the emulated frame down to fit the panel (ZCode's `fit` zoom): 1 while
 * it fits, a smaller factor when the frame is wider / taller than the canvas.
 * Reading layout of a zero-sized container (first paint, a hidden tab) reads as
 * "fits", so the frame is never scaled into nothing.
 */
const frameScale = computed(() => {
  const canvas = canvasRef.value;
  if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return 1;
  return Math.min(
    1,
    (canvas.clientWidth - 32) / viewport.value.width,
    (canvas.clientHeight - 32) / viewport.value.height
  );
});

/** In-flight drag (pointer capture; deltas divide by the render scale). */
const drag = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  start: { width: number; height: number };
  widthDirection: -1 | 0 | 1;
  heightDirection: -1 | 0 | 1;
} | null>(null);

/** Toggle free-size mode (entering keeps the instance's last size). */
const toggleResponsive = (): void => {
  store.setBrowserResponsive(stateKey.value, !responsive.value);
};

/**
 * Begin a resize drag: capture the pointer so the gesture survives leaving the
 * handle, and remember the size it started from.
 * @param handle The pressed handle.
 * @param event The pointerdown.
 */
const beginResize = (handle: (typeof RESIZE_HANDLES)[number], event: PointerEvent): void => {
  if (event.pointerType === 'mouse' && event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  try {
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  } catch {
    // Capture is best-effort (a browser without pointer capture still gets move events).
  }
  drag.value = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    start: { ...viewport.value },
    widthDirection: handle.widthDirection,
    heightDirection: handle.heightDirection
  };
};

/**
 * Apply a drag: the pointer delta in SCREEN px divided by the render scale, so
 * a scaled-down frame still follows the cursor 1:1.
 * @param event The pointermove.
 */
const onResizeMove = (event: PointerEvent): void => {
  const active = drag.value;
  if (!active || active.pointerId !== event.pointerId) return;
  if (event.pointerType === 'mouse' && (event.buttons & 1) === 0) {
    drag.value = null;
    return;
  }
  const scale = frameScale.value > 0 ? frameScale.value : 1;
  store.setBrowserViewport(stateKey.value, {
    width: active.start.width + ((event.clientX - active.startX) / scale) * active.widthDirection,
    height: active.start.height + ((event.clientY - active.startY) / scale) * active.heightDirection
  });
};

/**
 * End a drag (pointerup / cancel / lost capture).
 * @param event The ending event.
 */
const endResize = (event: PointerEvent): void => {
  if (drag.value && drag.value.pointerId === event.pointerId) drag.value = null;
};

/**
 * Resize from the keyboard (arrows; Shift moves 10px at a time), ZCode's own
 * affordance for the handles.
 * @param handle The focused handle.
 * @param event The keydown.
 */
const onResizeKey = (handle: (typeof RESIZE_HANDLES)[number], event: KeyboardEvent): void => {
  const step = event.shiftKey ? 10 : 1;
  const deltas: Record<string, [number, number]> = {
    ArrowLeft: [-1, 0],
    ArrowRight: [1, 0],
    ArrowUp: [0, -1],
    ArrowDown: [0, 1]
  };
  const delta = deltas[event.key];
  if (!delta) return;
  event.preventDefault();
  store.setBrowserViewport(stateKey.value, {
    width: viewport.value.width + delta[0] * step * (handle.widthDirection || 0),
    height: viewport.value.height + delta[1] * step * (handle.heightDirection || 0)
  });
};

/**
 * Open the page in a real browser window — the honest devtools story for an
 * iframe (a page cannot attach devtools to a frame; ZCode's button calls
 * Electron's `<webview>.openDevTools()` on its guest).
 */
const openExternal = (): void => {
  if (!pageUrl.value) return;
  window.open(pageUrl.value, '_blank', 'noopener,noreferrer');
};
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
      "framingHint": "有些站点禁止被内嵌（X-Frame-Options / CSP），这类页面会保持空白。",
      "freeSizeOn": "自由尺寸：在模拟设备框里预览页面",
      "freeSizeOff": "退出自由尺寸",
      "resizeHandle": "调整浏览器尺寸",
      "devtools": "打开调试工具",
      "devtoolsHint": "在新窗口打开该页面——跨域 iframe 无法内嵌开发者工具，新窗口里可用浏览器自带的调试工具"
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
      "framingHint": "Some sites refuse to be embedded (X-Frame-Options / CSP); those pages stay blank.",
      "freeSizeOn": "Free size: preview the page in an emulated device frame",
      "freeSizeOff": "Leave free size",
      "resizeHandle": "Resize the browser",
      "devtools": "Open DevTools",
      "devtoolsHint": "Open this page in a real window — a cross-origin iframe cannot host devtools, a window can"
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
      "framingHint": "一部のサイトは埋め込みを拒否します（X-Frame-Options / CSP）。その場合は空白のままです。",
      "freeSizeOn": "フリーサイズ：仮想デバイス枠でページをプレビュー",
      "freeSizeOff": "フリーサイズを終了",
      "resizeHandle": "ブラウザのサイズを変更",
      "devtools": "開発者ツールを開く",
      "devtoolsHint": "このページを新しいウィンドウで開きます — クロスオリジン iframe は開発者ツールを内蔵できないためです"
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
      "framingHint": "일부 사이트는 임베드를 거부합니다(X-Frame-Options / CSP). 그런 페이지는 빈 화면으로 남습니다.",
      "freeSizeOn": "자유 크기: 가상 기기 프레임에서 페이지 미리보기",
      "freeSizeOff": "자유 크기 종료",
      "resizeHandle": "브라우저 크기 조절",
      "devtools": "개발자 도구 열기",
      "devtoolsHint": "이 페이지를 새 창에서 엽니다 — 교차 출처 iframe은 개발자 도구를 내장할 수 없습니다"
    }
  }
}
</i18n>
