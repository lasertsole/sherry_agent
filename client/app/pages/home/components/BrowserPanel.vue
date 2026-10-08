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
        @click="navStep(-1)" />
      <Button
        icon="pi pi-arrow-right"
        size="small"
        text
        severity="secondary"
        :disabled="!canForward"
        :title="t('browser.forward')"
        :aria-label="t('browser.forward')"
        data-test="browser-forward"
        @click="navStep(1)" />
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        :disabled="!pageUrl || devtoolsOn"
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
      <!-- DevTools: in CDP mode this opens the real inspector target and the
           panel watches it (the backend builds the devtools:// frontend URL, so
           the debug port never leaves the process); in iframe mode a cross-origin
           frame cannot hand out devtools, so the honest substitute is opening the
           page in a real window (ZCode calls <webview>.openDevTools()). -->
      <Button
        :icon="devtoolsOn ? 'pi pi-arrow-left' : 'pi pi-external-link'"
        size="small"
        text
        :severity="devtoolsOn ? 'primary' : 'secondary'"
        :disabled="!pageUrl"
        :title="devtoolsOn ? t('browser.devtoolsReturn') : t('browser.devtoolsHint')"
        :aria-label="devtoolsOn ? t('browser.devtoolsReturn') : t('browser.devtools')"
        data-test="browser-devtools"
        @click="devtoolsAction" />
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
    <!-- Free-size controls: the frame's width / height and its scale, right under
         the address row and only while free size is on (ZCode's
         BrowserViewportToolbar shows its sizing controls the same way). -->
    <div
      v-if="responsive"
      class="flex shrink-0 flex-wrap items-center justify-center gap-2 border-b border-solid border-gray-100 px-2 py-1.5 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400"
      data-test="browser-size-row">
      <label class="flex items-center gap-1">
        <span>{{ t('browser.width') }}</span>
        <input
          v-model.number="draftWidth"
          type="number"
          :min="BROWSER_VIEWPORT_LIMITS.minWidth"
          :max="BROWSER_VIEWPORT_LIMITS.maxWidth"
          class="w-16 rounded border border-solid border-gray-200 bg-transparent px-1 py-0.5 font-mono text-[11px] text-theme-main outline-none dark:border-gray-700"
          data-test="browser-width-input"
          @change="applySize" />
      </label>
      <label class="flex items-center gap-1">
        <span>{{ t('browser.height') }}</span>
        <input
          v-model.number="draftHeight"
          type="number"
          :min="BROWSER_VIEWPORT_LIMITS.minHeight"
          :max="BROWSER_VIEWPORT_LIMITS.maxHeight"
          class="w-16 rounded border border-solid border-gray-200 bg-transparent px-1 py-0.5 font-mono text-[11px] text-theme-main outline-none dark:border-gray-700"
          data-test="browser-height-input"
          @change="applySize" />
      </label>
      <label class="flex items-center gap-1">
        <span>{{ t('browser.zoom') }}</span>
        <select
          :value="zoom"
          class="rounded border border-solid border-gray-200 bg-transparent px-1 py-0.5 text-[11px] text-theme-main outline-none dark:border-gray-700 dark:bg-gray-900"
          :aria-label="t('browser.zoom')"
          data-test="browser-zoom-input"
          @change="onZoomChange">
          <option
            v-for="option in BROWSER_ZOOM_OPTIONS"
            :key="option"
            :value="option">
            {{ option === 'fit' ? t('browser.zoomFit', { percent: Math.round(frameScale * 100) }) : `${option}%` }}
          </option>
        </select>
      </label>
    </div>

    <!-- CDP mode keeps one thin status line: a dropped backend reconnects on its
         own, and a refused command shows here instead of a toast storm. -->
    <div
      v-if="cdp && statusHint"
      class="flex shrink-0 items-center gap-1 border-b border-solid border-gray-100 px-2 py-1 text-[11px] text-amber-600 dark:border-gray-800 dark:text-amber-400"
      data-test="browser-status">
      <i
        class="pi text-[10px]"
        :class="connected ? 'pi-exclamation-circle' : 'pi-spin pi-spinner'" />
      <span class="min-w-0 truncate">{{ statusHint }}</span>
    </div>

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
            <img
              v-if="cdp && frameSrc"
              :src="frameSrc"
              class="h-full w-full border-0 outline-none"
              alt=""
              draggable="false"
              tabindex="0"
              data-test="browser-frame"
              @pointerdown="onPointer($event, 'down')"
              @pointerup="onPointer($event, 'up')"
              @pointermove="onPointer($event, 'move')"
              @wheel.prevent="onWheel"
              @keydown="onKeyDown"
              @keyup="onKeyUp" />
            <div
              v-else-if="cdp"
              class="flex h-full w-full items-center justify-center text-xs text-gray-400 dark:text-gray-500">
              {{ t('browser.connecting') }}
            </div>
            <iframe
              v-else
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
      <div
        v-else-if="cdp"
        class="h-full w-full outline-none"
        tabindex="0"
        data-test="browser-cdp-surface"
        @keydown="onKeyDown"
        @keyup="onKeyUp">
        <img
          v-if="frameSrc"
          :src="frameSrc"
          class="h-full w-full border-0 object-contain outline-none"
          alt=""
          draggable="false"
          data-test="browser-frame"
          @pointerdown="onPointer($event, 'down')"
          @pointerup="onPointer($event, 'up')"
          @pointermove="onPointer($event, 'move')"
          @wheel.prevent="onWheel" />
        <div
          v-else
          class="flex h-full items-center justify-center text-xs text-gray-400 dark:text-gray-500">
          {{ t('browser.connecting') }}
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
        <span
          v-if="!cdp"
          class="text-[11px]"
          >{{ t('browser.framingHint') }}</span
        >
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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
// Stable module specifiers so tests can vi.mock the channel / the bridge (the
// unimport injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { BrowserChannel } from '~/composables/browser-channel';
import { fetchBrowserStatus } from '~/composables/bridge/toolbox';
/* eslint-enable @typescript-eslint/no-restricted-imports */
// Stores are never auto-imported (unimport only walks app/composables).
import type { BrowserZoom } from '~/stores/toolbox';
import {
  BROWSER_VIEWPORT_LIMITS,
  BROWSER_ZOOM_OPTIONS,
  browserZoomScale,
  normalizeUrl,
  useToolboxStore
} from '~/stores/toolbox';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ payload?: { instance?: string } }>();

const route = useRoute();
const store = useToolboxStore();

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
 * CDP mode: the backend hosts a real Chromium and streams it here.
 *
 * The mode is chosen once per mount from `GET /browser/status` — a default
 * install (feature off) keeps the iframe panel exactly as it was. In CDP mode
 * the panel watches JPEG frames and forwards pointer / wheel / keyboard events;
 * the backend performs every browser call, so no debug port ever reaches here.
 */
const cdp = computed(() => store.browserFor(stateKey.value).cdp);
const connected = computed(() => store.browserFor(stateKey.value).connected);
const frame = computed(() => store.browserFor(stateKey.value).frame);
const frameSrc = computed(() => (frame.value ? `data:image/jpeg;base64,${frame.value.data}` : ''));
const devtoolsOn = computed(() => store.browserFor(stateKey.value).devtools);
/**
 * The CSS-pixel width input events are expressed in.
 *
 * Free size emulates a device server-side, so the page's CSS pixels are the
 * STORE's viewport — a screencast frame can come back at a different pixel size
 * (Chrome's mobile emulation reports its own scale, measured 393x852 -> 554x1200),
 * and mapping with that size lands clicks off-target. Without the override the
 * frame's own size is the only truth.
 */
const inputWidth = computed(() =>
  responsive.value ? viewport.value.width : (frame.value?.width ?? viewport.value.width)
);
/** The channel instance for this panel (not reactive: it owns a socket). */
let channel: BrowserChannel | null = null;
const lastError = ref('');
/** True once the socket has been up — a drop afterwards is what deserves the hint. */
const everConnected = ref(false);
let errorTimer: ReturnType<typeof setTimeout> | null = null;

/** The status line's text: a transient error, or a drop we are retrying. */
const statusHint = computed(() => {
  if (lastError.value) return lastError.value;
  if (cdp.value && everConnected.value && !connected.value) return t('browser.reconnecting');
  return '';
});

/**
 * Show one refused command in the status line, then fade it.
 * @param message
 */
function showError(message: string): void {
  lastError.value = message;
  if (errorTimer) clearTimeout(errorTimer);
  errorTimer = setTimeout(() => {
    lastError.value = '';
    errorTimer = null;
  }, 4000);
}

/**
 * Apply a `page` frame (navigation / history move / watch switch).
 * @param next
 * @param next.url
 * @param next.can_back
 * @param next.can_forward
 * @param next.kind
 */
function applyPage(next: { url: string; can_back: boolean; can_forward: boolean; kind?: string }): void {
  everConnected.value = true;
  store.setBrowserNav(stateKey.value, {
    url: next.url,
    canBack: next.can_back,
    canForward: next.can_forward
  });
  // The frame names what is being watched, so the devtools toggle follows the
  // server's truth instead of guessing.
  store.setBrowserDevtools(stateKey.value, next.kind === 'devtools');
  if (next.url) lastError.value = '';
}

/**
 * Walk the page's history (CDP) or the panel's own list (iframe).
 * @param direction
 */
function navStep(direction: -1 | 1): void {
  if (cdp.value) {
    channel?.send({ event: direction === -1 ? 'back' : 'forward' });
    return;
  }
  store.step(stateKey.value, direction);
}

/** The devtools button: the inspector (CDP) or a real window (iframe). */
function devtoolsAction(): void {
  if (!cdp.value) {
    openExternal();
    return;
  }
  const next = !devtoolsOn.value;
  store.setBrowserDevtools(stateKey.value, next); // optimistic; the page frame confirms
  channel?.send({ event: 'devtools', on: next });
}

/**
 * CDP modifier bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8 — CDP's own order).
 * @param event
 * @param event.altKey
 * @param event.ctrlKey
 * @param event.metaKey
 * @param event.shiftKey
 */
function modifierBits(event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
}

/**
 * The page's own CSS coordinates for a pointer / wheel event on the picture.
 * @param event
 * @param target
 */
function pageCoords(event: PointerEvent | WheelEvent, target: HTMLElement): { x: number; y: number } {
  const rect = target.getBoundingClientRect();
  const scale = rect.width > 0 ? rect.width / inputWidth.value : 1;
  return { x: (event.clientX - rect.left) / scale, y: (event.clientY - rect.top) / scale };
}

/** Double-click detection (PointerEvent.detail is not reliable for this). */
let lastClick = { at: 0, x: 0, y: 0 };

/**
 * Forward one pointer event (double clicks become clickCount=2).
 * @param event
 * @param phase
 */
function onPointer(event: PointerEvent, phase: 'down' | 'up' | 'move'): void {
  if (!cdp.value) return;
  const target = event.currentTarget as HTMLElement;
  const { x, y } = pageCoords(event, target);
  let clickCount = 0;
  if (phase === 'down') {
    const now = Date.now();
    const closeToLast = Math.abs(x - lastClick.x) < 8 && Math.abs(y - lastClick.y) < 8;
    clickCount = now - lastClick.at < 400 && closeToLast ? 2 : 1;
    lastClick = { at: now, x, y };
    // A click must reach the panel's key handler too (focus follows the pointer).
    const surface = target.closest('[tabindex]') as HTMLElement | null;
    surface?.focus?.();
    event.preventDefault();
  }
  channel?.send({
    event: 'input',
    kind: 'mouse',
    payload: {
      type: phase === 'down' ? 'mousePressed' : phase === 'up' ? 'mouseReleased' : 'mouseMoved',
      x,
      y,
      button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left',
      buttons: event.buttons,
      clickCount,
      modifiers: modifierBits(event),
      pointerType: event.pointerType || 'mouse'
    }
  });
}

/**
 * Forward one wheel event (page coordinates, pixel deltas).
 * @param event
 */
function onWheel(event: WheelEvent): void {
  if (!cdp.value) return;
  const target = event.currentTarget as HTMLElement;
  const { x, y } = pageCoords(event, target);
  channel?.send({
    event: 'input',
    kind: 'wheel',
    payload: { x, y, deltaX: event.deltaX, deltaY: event.deltaY, modifiers: modifierBits(event) }
  });
}

/** The named keys CDP wants a code + virtual key for. */
const SPECIAL_KEYS: Record<string, { code: string; vk: number }> = {
  Enter: { code: 'Enter', vk: 13 },
  Tab: { code: 'Tab', vk: 9 },
  Escape: { code: 'Escape', vk: 27 },
  Backspace: { code: 'Backspace', vk: 8 },
  Delete: { code: 'Delete', vk: 46 },
  ArrowUp: { code: 'ArrowUp', vk: 38 },
  ArrowDown: { code: 'ArrowDown', vk: 40 },
  ArrowLeft: { code: 'ArrowLeft', vk: 37 },
  ArrowRight: { code: 'ArrowRight', vk: 39 },
  PageUp: { code: 'PageUp', vk: 33 },
  PageDown: { code: 'PageDown', vk: 34 },
  Home: { code: 'Home', vk: 36 },
  End: { code: 'End', vk: 35 },
  ' ': { code: 'Space', vk: 32 }
};

/**
 * Forward one key event; printable characters ride `text` so the page gets a char.
 * @param type
 * @param event
 */
function sendKey(type: 'keyDown' | 'keyUp', event: KeyboardEvent): void {
  const special = SPECIAL_KEYS[event.key];
  const payload: Record<string, unknown> = {
    type,
    key: event.key,
    modifiers: modifierBits(event)
  };
  if (special) {
    payload.code = special.code;
    payload.windowsVirtualKeyCode = special.vk;
  } else if (event.key.length === 1) {
    payload.code = `Key${event.key.toUpperCase()}`;
    payload.windowsVirtualKeyCode = event.key.toUpperCase().charCodeAt(0);
    if (type === 'keyDown' && !event.ctrlKey && !event.metaKey) payload.text = event.key;
  }
  channel?.send({ event: 'input', kind: 'key', payload });
}

/**
 * Key handler on the CDP surface (nothing types locally, so prevent default).
 * @param event
 */
function onKeyDown(event: KeyboardEvent): void {
  if (!cdp.value) return;
  event.preventDefault();
  sendKey('keyDown', event);
}
function onKeyUp(event: KeyboardEvent): void {
  if (!cdp.value) return;
  sendKey('keyUp', event);
}

/** Free-size changes drive real device emulation in CDP mode (80 ms trailing). */
let viewportTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleViewport(): void {
  if (viewportTimer) clearTimeout(viewportTimer);
  viewportTimer = setTimeout(() => {
    viewportTimer = null;
    if (!cdp.value || !channel) return;
    if (responsive.value) {
      channel.send({
        event: 'viewport',
        width: viewport.value.width,
        height: viewport.value.height,
        scale: 1,
        mobile: viewport.value.width < 700
      });
    } else {
      // Leaving free size restores a desktop frame (the emulation would otherwise
      // keep the phone-sized viewport the user just left).
      channel.send({ event: 'viewport', width: 1280, height: 900, scale: 1, mobile: false });
    }
  }, 80);
}

onMounted(async () => {
  if (!sessionId.value) return;
  const status = await fetchBrowserStatus();
  store.setBrowserCdp(stateKey.value, status.enabled);
  if (!status.enabled) return;
  channel = new BrowserChannel(sessionId.value, {
    onReady: ready => {
      store.setBrowserCdp(stateKey.value, ready.enabled);
      store.setBrowserConnected(stateKey.value, true);
      applyPage(ready);
    },
    onPage: applyPage,
    onFrame: next => store.setBrowserFrame(stateKey.value, next),
    onError: showError,
    onOpen: () => {
      everConnected.value = true;
      store.setBrowserConnected(stateKey.value, true);
    },
    onClose: () => {
      store.setBrowserConnected(stateKey.value, false);
      store.setBrowserFrame(stateKey.value, null);
    }
  });
  channel.connect();
});

onUnmounted(() => {
  channel?.dispose();
  channel = null;
  store.setBrowserConnected(stateKey.value, false);
  if (viewportTimer) clearTimeout(viewportTimer);
  if (errorTimer) clearTimeout(errorTimer);
});

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
 * The scale the frame is drawn at: an explicit zoom percentage, or — for `fit` —
 * down to the panel (1 while it fits). A zero-sized container (the first paint,
 * a hidden tab) reads as "fits", so nothing is scaled into nothing.
 */
const frameScale = computed(() => {
  const explicit = browserZoomScale(zoom.value);
  if (explicit !== null) return explicit;
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

/** The zoom picker's value (per instance). */
const zoom = computed(() => store.browserFor(stateKey.value).zoom);

watch([viewport, zoom, responsive], scheduleViewport, { deep: true });

/**
 * The width / height inputs' text. Drafts, not the source of truth: a user types
 * intermediate values ("39" on the way to "390") that must not resize the frame,
 * so the store only sees what a `change` (Enter / blur) confirmed.
 */
const draftWidth = ref(viewport.value.width);
const draftHeight = ref(viewport.value.height);
watch(
  viewport,
  next => {
    draftWidth.value = next.width;
    draftHeight.value = next.height;
  },
  { deep: true }
);

/** Commit the typed size (clamped by the store) and echo the clamped value back. */
const applySize = (): void => {
  store.setBrowserViewport(stateKey.value, {
    width: Number(draftWidth.value) || viewport.value.width,
    height: Number(draftHeight.value) || viewport.value.height
  });
  draftWidth.value = store.browserFor(stateKey.value).viewport.width;
  draftHeight.value = store.browserFor(stateKey.value).viewport.height;
};

/**
 * Apply a zoom choice from the picker.
 * @param event The select's change.
 */
const onZoomChange = (event: Event): void => {
  store.setBrowserZoom(stateKey.value, (event.target as HTMLSelectElement).value as BrowserZoom);
};

/** Toggle free-size mode (entering keeps the instance's last size / zoom). */
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

/** A few one-click targets for the empty state (the address bar takes any URL). */
const QUICK_LINKS: ReadonlyArray<{ label: string; url: string }> = [
  { label: 'GitHub', url: 'https://github.com' },
  { label: 'MDN', url: 'https://developer.mozilla.org' },
  { label: 'localhost:8080', url: 'http://127.0.0.1:8080' }
];

const pageUrl = computed(() => store.browserFor(stateKey.value).url);
/** CDP mode reads the page's OWN history (the server's answer); iframe mode
 *  walks the panel's client-side list. */
const canBack = computed(() =>
  cdp.value ? store.browserFor(stateKey.value).serverCanBack : store.canGoBack(stateKey.value)
);
const canForward = computed(() =>
  cdp.value ? store.browserFor(stateKey.value).serverCanForward : store.canGoForward(stateKey.value)
);
/** The address bar mirrors the page, but only after a navigation settles — a
 *  half-typed URL must not be overwritten while the user is typing. */
const address = ref(pageUrl.value);
watch(pageUrl, next => {
  address.value = next;
});
/** Bumped on a reload: the iframe re-mounts on a new key (an `src` set to the
 *  same URL is a no-op for the browser). */
const frameKey = ref(0);

/** Navigate to what the address bar holds (CDP mode sends it over the channel). */
const go = (): void => {
  if (cdp.value) {
    channel?.send({ event: 'nav', url: normalizeUrl(address.value) });
    return;
  }
  store.navigate(stateKey.value, address.value);
};
/** Re-enter the current URL (the panel's 刷新). */
const reload = (): void => {
  if (cdp.value) {
    channel?.send({ event: 'reload' });
    return;
  }
  frameKey.value += 1;
};
/**
 * Jump to one of the empty state's quick links.
 * @param url
 */
const quickLink = (url: string): void => {
  address.value = url;
  go();
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
      "width": "宽",
      "height": "高",
      "zoom": "缩放",
      "zoomFit": "适应（{percent}%）",
      "freeSizeOff": "退出自由尺寸",
      "resizeHandle": "调整浏览器尺寸",
      "devtools": "打开调试工具",
      "devtoolsHint": "在新窗口打开该页面——跨域 iframe 无法内嵌开发者工具，新窗口里可用浏览器自带的调试工具",
      "devtoolsReturn": "返回页面",
      "connecting": "正在连接浏览器…",
      "reconnecting": "连接已断开，正在重连…"
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
      "width": "W",
      "height": "H",
      "zoom": "Zoom",
      "zoomFit": "Fit ({percent}%)",
      "freeSizeOff": "Leave free size",
      "resizeHandle": "Resize the browser",
      "devtools": "Open DevTools",
      "devtoolsHint": "Open this page in a real window — a cross-origin iframe cannot host devtools, a window can",
      "devtoolsReturn": "Back to the page",
      "connecting": "Connecting to the browser…",
      "reconnecting": "Connection lost — reconnecting…"
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
      "width": "幅",
      "height": "高さ",
      "zoom": "ズーム",
      "zoomFit": "フィット（{percent}%）",
      "freeSizeOff": "フリーサイズを終了",
      "resizeHandle": "ブラウザのサイズを変更",
      "devtools": "開発者ツールを開く",
      "devtoolsHint": "このページを新しいウィンドウで開きます — クロスオリジン iframe は開発者ツールを内蔵できないためです",
      "devtoolsReturn": "ページに戻る",
      "connecting": "ブラウザに接続中…",
      "reconnecting": "接続が切れました。再接続中…"
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
      "width": "너비",
      "height": "높이",
      "zoom": "줌",
      "zoomFit": "맞춤({percent}%)",
      "freeSizeOff": "자유 크기 종료",
      "resizeHandle": "브라우저 크기 조절",
      "devtools": "개발자 도구 열기",
      "devtoolsHint": "이 페이지를 새 창에서 엽니다 — 교차 출처 iframe은 개발자 도구를 내장할 수 없습니다",
      "devtoolsReturn": "페이지로 돌아가기",
      "connecting": "브라우저에 연결하는 중…",
      "reconnecting": "연결이 끊겼습니다. 다시 연결하는 중…"
    }
  }
}
</i18n>
