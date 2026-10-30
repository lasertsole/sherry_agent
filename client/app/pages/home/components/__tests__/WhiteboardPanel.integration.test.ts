/**
 * The whiteboard panel (integration).
 *
 * Contract: the canvas is the drawing surface (pointerdown → move → up commits
 * ONE stroke with normalized points), undo/redo/clear act on the board store,
 * "add to chat" exports a PNG and hands it to the page over the mitt bus (the
 * send path re-reads the tray, so the image must arrive there), and a browser
 * without Canvas2D shows the fallback instead of crashing.
 *
 * happy-dom has no canvas implementation, so the 2D context and `toDataURL`
 * are recorded stubs.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import WhiteboardPanel from '@/pages/home/components/WhiteboardPanel.vue';
import { useWhiteboardStore } from '@/stores/whiteboard';
import { useRightSidebarStore } from '@/stores/right-sidebar';

const mitt = vi.hoisted(() => ({ emit: vi.fn(), on: vi.fn(), off: vi.fn() }));
vi.mock('~/composables/mitt', () => mitt);

/** A recording stand-in for CanvasRenderingContext2D. */
function fakeContext() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    fillRect: vi.fn(),
    setTransform: vi.fn(),
    lineCap: '',
    lineJoin: '',
    lineWidth: 0,
    strokeStyle: '',
    fillStyle: ''
  } as unknown as CanvasRenderingContext2D;
}

const canvasProto = HTMLCanvasElement.prototype as unknown as {
  getContext: ((contextId: string) => unknown) | undefined;
  toDataURL: ((type?: string) => string) | undefined;
};
const originalGetContext = canvasProto.getContext;
const originalToDataUrl = canvasProto.toDataURL;

let ctx: CanvasRenderingContext2D;

/** The canvas's CSS box (happy-dom reports zeros). */
const RECT = {
  left: 0,
  top: 0,
  width: 640,
  height: 400,
  right: 640,
  bottom: 400,
  x: 0,
  y: 0,
  toJSON: () => ({})
} as DOMRect;

/**
 * Mount the panel for one board.
 * @param boardId Board key.
 */
function mountPanel(boardId = 'board-test') {
  const wrapper = mount(WhiteboardPanel, { props: { payload: { boardId } } });
  const canvas = wrapper.find('[data-test="whiteboard-canvas"]').element as HTMLCanvasElement;
  canvas.getBoundingClientRect = () => RECT;
  return wrapper;
}

describe('WhiteboardPanel.vue', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    ctx = fakeContext();
    canvasProto.getContext = vi.fn(() => ctx);
    canvasProto.toDataURL = vi.fn(() => 'data:image/png;base64,QUJD');
    mitt.emit.mockClear();
  });

  afterEach(() => {
    canvasProto.getContext = originalGetContext;
    canvasProto.toDataURL = originalToDataUrl;
    vi.restoreAllMocks();
  });

  it('commits one stroke per pointer gesture with normalized points', async () => {
    const wrapper = mountPanel();
    const canvas = wrapper.find('[data-test="whiteboard-canvas"]');

    await canvas.trigger('pointerdown', { clientX: 0, clientY: 0, pointerId: 1 });
    await canvas.trigger('pointermove', { clientX: 320, clientY: 200, pointerId: 1 });
    await canvas.trigger('pointermove', { clientX: 640, clientY: 400, pointerId: 1 });
    await canvas.trigger('pointerup', { pointerId: 1 });

    const board = useWhiteboardStore().ensureBoard('board-test');
    expect(board.strokes).toHaveLength(1);
    expect(board.strokes[0]!.points).toEqual([
      { x: 0, y: 0 },
      { x: 0.5, y: 0.5 },
      { x: 1, y: 1 }
    ]);
    // The draft is gone and the view no longer shows the empty hint.
    expect(wrapper.find('[data-test="whiteboard-empty"]').exists()).toBe(false);
  });

  it('undoes, redoes and clears through the toolbar', async () => {
    const wrapper = mountPanel();
    const canvas = wrapper.find('[data-test="whiteboard-canvas"]');
    await canvas.trigger('pointerdown', { clientX: 10, clientY: 10, pointerId: 1 });
    await canvas.trigger('pointerup', { pointerId: 1 });
    const store = useWhiteboardStore();
    expect(store.ensureBoard('board-test').strokes).toHaveLength(1);

    await wrapper.find('[data-test="whiteboard-undo"]').trigger('click');
    expect(store.ensureBoard('board-test').strokes).toHaveLength(0);
    expect(store.ensureBoard('board-test').undoneStrokes).toHaveLength(1);

    await wrapper.find('[data-test="whiteboard-redo"]').trigger('click');
    expect(store.ensureBoard('board-test').strokes).toHaveLength(1);

    await wrapper.find('[data-test="whiteboard-clear"]').trigger('click');
    expect(store.ensureBoard('board-test').strokes).toHaveLength(0);
    expect(wrapper.find('[data-test="whiteboard-empty"]').exists()).toBe(true);
  });

  it('applies the selected colour, eraser tool and width to the next stroke', async () => {
    const wrapper = mountPanel();
    // The toolbar is plain buttons/inputs, so no component stubs are needed.
    await wrapper.find('[data-test="whiteboard-tool-eraser"]').trigger('click');
    await wrapper.find('[data-test="whiteboard-color-1971c2"]').trigger('click');
    await wrapper.find('[data-test="whiteboard-width"]').setValue('12');

    const canvas = wrapper.find('[data-test="whiteboard-canvas"]');
    await canvas.trigger('pointerdown', { clientX: 0, clientY: 0, pointerId: 3 });
    await canvas.trigger('pointerup', { pointerId: 3 });

    const stroke = useWhiteboardStore().ensureBoard('board-test').strokes[0]!;
    expect(stroke.tool).toBe('eraser');
    expect(stroke.color).toBe('#1971c2');
    expect(stroke.width).toBe(12);
  });

  it('hands the drawing to the chat as a PNG the page can queue as an image', async () => {
    const rightSidebar = useRightSidebarStore();
    rightSidebar.setActiveSession('session-42');
    const wrapper = mountPanel();

    const canvas = wrapper.find('[data-test="whiteboard-canvas"]');
    await canvas.trigger('pointerdown', { clientX: 0, clientY: 0, pointerId: 1 });
    await canvas.trigger('pointerup', { pointerId: 1 });
    await wrapper.find('[data-test="whiteboard-name"]').setValue('sketch');
    await wrapper.find('[data-test="whiteboard-name"]').trigger('change');
    await wrapper.find('[data-test="whiteboard-add-to-chat"]').trigger('click');
    await flushPromises();

    expect(mitt.emit).toHaveBeenCalledWith('chat:add-image', {
      sessionId: 'session-42',
      payload: { base64: 'QUJD', name: 'sketch.png' }
    });
  });

  it('falls back to a message when Canvas2D is unavailable', async () => {
    canvasProto.getContext = vi.fn(() => null);

    const wrapper = mountPanel();
    // onMounted flips the flag; the DOM updates on the next tick.
    await nextTick();

    expect(wrapper.find('[data-test="whiteboard-unsupported"]').text()).toContain('Canvas');
    expect(wrapper.find('[data-test="whiteboard-empty"]').exists()).toBe(false);
  });

  it('keeps one board per board id (two panels do not share strokes)', async () => {
    const first = mountPanel('one');
    await first
      .find('[data-test="whiteboard-canvas"]')
      .trigger('pointerdown', { clientX: 5, clientY: 5, pointerId: 1 });
    await first.find('[data-test="whiteboard-canvas"]').trigger('pointerup', { pointerId: 1 });

    const second = mountPanel('two');

    const store = useWhiteboardStore();
    expect(store.ensureBoard('one').strokes).toHaveLength(1);
    expect(store.ensureBoard('two').strokes).toHaveLength(0);
    expect(second.find('[data-test="whiteboard-empty"]').exists()).toBe(true);
  });
});
