<template>
  <div
    class="flex h-full min-h-0 flex-col"
    data-test="whiteboard-panel">
    <!-- Toolbar: identity, tool, colour, width, history, hand-off to the chat.
         Plain controls where PrimeVue would need a menu/portal (swatches, the
         range input) and PrimeVue Buttons for the icon actions, matching the
         other panels' idiom. -->
    <!-- `w-full min-w-0` is load-bearing: without a constrained width the row
         is sized by its content and overflows the panel (the send action ended
         up off-screen at 1280px) instead of wrapping. -->
    <div
      class="flex w-full min-w-0 shrink-0 flex-wrap items-center gap-1.5 border-b border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800">
      <i class="pi pi-pencil text-theme-main text-xs"></i>
      <input
        v-model="boardName"
        type="text"
        class="w-20 min-w-0 shrink rounded border border-solid border-gray-200 bg-transparent px-1.5 py-0.5 text-[11px] outline-none focus:border-theme-main dark:border-gray-700"
        :placeholder="t('whiteboard.namePlaceholder')"
        :aria-label="t('whiteboard.namePlaceholder')"
        data-test="whiteboard-name"
        @change="rename" />
      <div class="flex items-center gap-0.5">
        <button
          v-for="option in TOOLS"
          :key="option.value"
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded text-[11px]"
          :class="
            tool === option.value
              ? 'bg-theme-main/15 text-theme-main'
              : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
          "
          :title="t(option.labelKey)"
          :aria-label="t(option.labelKey)"
          :aria-pressed="tool === option.value"
          :data-test="`whiteboard-tool-${option.value}`"
          @click="tool = option.value">
          <i :class="option.icon"></i>
        </button>
      </div>
      <div class="flex items-center gap-1">
        <button
          v-for="color in COLORS"
          :key="color"
          type="button"
          class="h-4 w-4 rounded-full border border-solid border-black/10"
          :class="currentColor === color ? 'ring-2 ring-theme-main ring-offset-1 dark:ring-offset-gray-900' : ''"
          :style="{ background: color }"
          :aria-label="color"
          :data-test="`whiteboard-color-${color.replace('#', '')}`"
          @click="currentColor = color" />
      </div>
      <input
        v-model.number="lineWidth"
        type="range"
        class="h-4 w-16 min-w-0 accent-theme-main"
        :min="MIN_WIDTH"
        :max="MAX_WIDTH"
        :step="1"
        :aria-label="t('whiteboard.width')"
        data-test="whiteboard-width" />
      <span
        class="w-5 text-right text-[10px] tabular-nums text-gray-500 dark:text-gray-400"
        data-test="whiteboard-width-value"
        >{{ lineWidth }}</span
      >
      <span class="mx-0.5 h-4 w-px bg-gray-200 dark:bg-gray-700"></span>
      <Button
        icon="pi pi-undo"
        size="small"
        text
        severity="secondary"
        :disabled="!canUndo"
        :title="t('whiteboard.undo')"
        :aria-label="t('whiteboard.undo')"
        data-test="whiteboard-undo"
        @click="undo" />
      <Button
        icon="pi pi-redo"
        size="small"
        text
        severity="secondary"
        :disabled="!canRedo"
        :title="t('whiteboard.redo')"
        :aria-label="t('whiteboard.redo')"
        data-test="whiteboard-redo"
        @click="redo" />
      <Button
        icon="pi pi-trash"
        size="small"
        text
        severity="secondary"
        :disabled="!canUndo"
        :title="t('whiteboard.clear')"
        :aria-label="t('whiteboard.clear')"
        data-test="whiteboard-clear"
        @click="clear" />
      <Button
        icon="pi pi-send"
        size="small"
        text
        severity="secondary"
        :disabled="strokes.length === 0"
        :title="t('whiteboard.addToChat')"
        :aria-label="t('whiteboard.addToChat')"
        data-test="whiteboard-add-to-chat"
        @click="addToChat" />
    </div>

    <!-- Board: a fixed-ratio drawing surface that scales with the column. The
         logical size is 1280×800; points are normalized, so CSS size changes
         never alter the drawing. -->
    <div class="relative min-h-0 flex-1 overflow-auto bg-gray-100 p-2 dark:bg-gray-800">
      <div
        class="relative mx-auto w-full overflow-hidden rounded-md shadow-sm"
        style="aspect-ratio: 16 / 10">
        <canvas
          ref="canvasRef"
          class="absolute inset-0 h-full w-full touch-none"
          :class="tool === 'eraser' ? 'cursor-cell' : 'cursor-crosshair'"
          data-test="whiteboard-canvas"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @pointerleave="onPointerUp" />
        <div
          v-if="unsupported"
          class="pointer-events-none absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-gray-500 dark:text-gray-400"
          data-test="whiteboard-unsupported">
          {{ t('whiteboard.unsupported') }}
        </div>
        <div
          v-else-if="strokes.length === 0"
          class="pointer-events-none absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-gray-400 dark:text-gray-500"
          data-test="whiteboard-empty">
          {{ t('whiteboard.empty') }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useWhiteboardStore } from '~/stores/whiteboard';
import { useRightSidebarStore } from '~/stores/right-sidebar';
// Only the TYPES are imported: the canvas helpers, `extractBase64` and `emit`
// are Nuxt auto-imports (bare symbols), same as every other composable call.
import type { WhiteboardPoint, WhiteboardStroke, WhiteboardTool } from '~/composables/use-whiteboard-canvas';
import { logUtil } from '~/utils/log';

const props = defineProps<{ payload?: { boardId?: string } }>();

const { t } = useI18n();
const store = useWhiteboardStore();
const rightSidebar = useRightSidebarStore();

/** Board key: one board per tab identity (the shell's payload), else `default`. */
const boardId = computed(() => props.payload?.boardId ?? 'default');

const TOOLS: ReadonlyArray<{ value: WhiteboardTool; labelKey: string; icon: string }> = [
  { value: 'pen', labelKey: 'whiteboard.pen', icon: 'pi pi-pencil' },
  { value: 'eraser', labelKey: 'whiteboard.eraser', icon: 'pi pi-eraser' }
];
const COLORS = WHITEBOARD_COLORS;
const MIN_WIDTH = WHITEBOARD_MIN_WIDTH;
const MAX_WIDTH = WHITEBOARD_MAX_WIDTH;

const board = computed(() => store.ensureBoard(boardId.value));
const strokes = computed<WhiteboardStroke[]>(() => board.value.strokes);
const canUndo = computed(() => board.value.strokes.length > 0);
const canRedo = computed(() => board.value.undoneStrokes.length > 0);

const boardName = ref(board.value.name);
const tool = ref<WhiteboardTool>('pen');
const currentColor = ref<string>(COLORS[0]);
const lineWidth = ref(4);
const draft = ref<WhiteboardStroke | null>(null);
const unsupported = ref(false);
const canvasRef = ref<HTMLCanvasElement | null>(null);
/** Live 2D context; null = this browser has no Canvas2D (F3). */
let ctx: CanvasRenderingContext2D | null = null;

/** Repaint paper + committed strokes + the in-progress stroke. */
function redraw(): void {
  if (!ctx) return;
  drawDocument(ctx, strokes.value, draft.value);
}

/**
 * Pointer position in normalized canvas coordinates.
 * @param event Pointer event.
 */
function pointOf(event: PointerEvent): WhiteboardPoint {
  const canvas = canvasRef.value as HTMLCanvasElement;
  return normalizePoint(event.clientX, event.clientY, canvas.getBoundingClientRect());
}

/**
 * Start a stroke and keep receiving moves after the pointer leaves the canvas.
 * @param event
 */
function onPointerDown(event: PointerEvent): void {
  if (unsupported.value) return;
  event.preventDefault();
  // Capture keeps the stroke alive when the pointer leaves the canvas. A
  // synthetic event (or a browser that has already released the pointer id)
  // can throw here; losing capture must not lose the stroke.
  try {
    (event.currentTarget as HTMLCanvasElement | null)?.setPointerCapture?.(event.pointerId);
  } catch {
    // fall through: moves still arrive while the pointer is over the canvas
  }
  draft.value = {
    tool: tool.value,
    color: currentColor.value,
    width: lineWidth.value,
    points: [pointOf(event)]
  };
  redraw();
}

/**
 * Extend the in-progress stroke (no-op without one).
 * @param event
 */
function onPointerMove(event: PointerEvent): void {
  if (!draft.value) return;
  event.preventDefault();
  draft.value.points.push(pointOf(event));
  redraw();
}

/** Commit the stroke (also on pointercancel / pointerleave). */
function onPointerUp(): void {
  const stroke = draft.value;
  draft.value = null;
  if (!stroke) return;
  if (!store.addStroke(boardId.value, stroke)) {
    toastWarn(t('whiteboard.maxStrokes'));
  }
  redraw();
}

/** Undo the last stroke. */
function undo(): void {
  store.undo(boardId.value);
  redraw();
}

/** Re-apply the last undone stroke. */
function redo(): void {
  store.redo(boardId.value);
  redraw();
}

/** Wipe the board (the undo stack goes with it). */
function clear(): void {
  store.clear(boardId.value);
  redraw();
}

/** Persist the display name (it also names the exported PNG). */
function rename(): void {
  store.renameBoard(boardId.value, boardName.value);
}

/**
 * Hand the drawing to the chat as an IMAGE attachment.
 *
 * The image state lives in the session page (`selectedImages`), and the send
 * path RE-READS that tray (a directly built MultiModalMessage is overwritten),
 * so the board mirrors the file-mention relay: emit → the page pushes the image
 * into the tray → preview chip, send and upload work unchanged.
 */
function addToChat(): void {
  if (strokes.value.length === 0) return;
  const dataUrl = exportPngDataUrl(strokes.value);
  const base64 = dataUrl ? extractBase64(dataUrl) : '';
  if (!base64) {
    toastWarn(t('whiteboard.exportFailed'));
    logUtil.w('[whiteboard] PNG export returned no data');
    return;
  }
  const name = `${boardName.value.trim() || 'whiteboard'}.png`;
  emit('chat:add-image', {
    sessionId: rightSidebar.activeSessionId,
    payload: { base64, name }
  });
  toastSuccess(t('whiteboard.addedToChat'));
}

onMounted(() => {
  const canvas = canvasRef.value;
  if (!canvas) return;
  ctx = setupCanvasDpi(canvas);
  if (!ctx) {
    unsupported.value = true;
    return;
  }
  redraw();
});

// Another board id (a tab re-keyed to a different board) starts on that board.
watch(boardId, id => {
  const next = store.ensureBoard(id);
  boardName.value = next.name;
  draft.value = null;
  redraw();
});
</script>
