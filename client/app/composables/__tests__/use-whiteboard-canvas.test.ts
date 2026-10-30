/**
 * The whiteboard's Canvas2D core (unit).
 *
 * Pins the drawing contract: a stroke is a normalized polyline; a one-point
 * stroke is a dot; the eraser paints the paper colour; the export always uses
 * the logical 1280x800 size (never the on-screen DPI size).
 */

import { describe, expect, it, vi } from 'vitest';
import {
  WHITEBOARD_BACKGROUND,
  WHITEBOARD_HEIGHT,
  WHITEBOARD_WIDTH,
  drawDocument,
  drawStroke,
  exportPngDataUrl,
  normalizePoint,
  setupCanvasDpi,
  type WhiteboardStroke
} from '@/composables/use-whiteboard-canvas';

/** A recording stand-in for CanvasRenderingContext2D (happy-dom has no canvas). */
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

/**
 * Count the recorded calls of one method.
 * @param ctx
 * @param name
 */
function calls(ctx: CanvasRenderingContext2D, name: keyof CanvasRenderingContext2D) {
  return (ctx[name] as unknown as { mock: { calls: unknown[] } }).mock.calls;
}

function stroke(overrides: Partial<WhiteboardStroke> = {}): WhiteboardStroke {
  return {
    tool: 'pen',
    color: '#e03131',
    width: 4,
    points: [
      { x: 0.25, y: 0.5 },
      { x: 0.5, y: 0.5 }
    ],
    ...overrides
  };
}

describe('drawStroke', () => {
  it('paints a normalized polyline at logical pixel coordinates', () => {
    const ctx = fakeContext();

    drawStroke(ctx, stroke());

    expect(ctx.lineCap).toBe('round');
    expect(ctx.lineJoin).toBe('round');
    expect(ctx.lineWidth).toBe(4);
    expect(ctx.strokeStyle).toBe('#e03131');
    expect(calls(ctx, 'moveTo')).toEqual([[0.25 * WHITEBOARD_WIDTH, 0.5 * WHITEBOARD_HEIGHT]]);
    expect(calls(ctx, 'lineTo')).toEqual([[0.5 * WHITEBOARD_WIDTH, 0.5 * WHITEBOARD_HEIGHT]]);
    expect(calls(ctx, 'stroke')).toHaveLength(1);
  });

  it('paints a single point as a filled dot', () => {
    const ctx = fakeContext();

    drawStroke(ctx, stroke({ points: [{ x: 0.5, y: 0.5 }] }));

    expect(calls(ctx, 'arc')).toHaveLength(1);
    expect(calls(ctx, 'fill')).toHaveLength(1);
    expect(calls(ctx, 'stroke')).toHaveLength(0);
  });

  it('erases with the paper colour instead of the stroke colour', () => {
    const ctx = fakeContext();

    drawStroke(ctx, stroke({ tool: 'eraser' }));

    expect(ctx.strokeStyle).toBe(WHITEBOARD_BACKGROUND);
  });

  it('ignores an empty stroke', () => {
    const ctx = fakeContext();

    drawStroke(ctx, stroke({ points: [] }));

    expect(calls(ctx, 'stroke')).toHaveLength(0);
    expect(calls(ctx, 'moveTo')).toHaveLength(0);
  });
});

describe('drawDocument', () => {
  it('repaints the paper, then every committed stroke, then the draft', () => {
    const ctx = fakeContext();
    const draft = stroke({ color: '#1971c2', points: [{ x: 0.9, y: 0.9 }] });

    drawDocument(ctx, [stroke(), stroke()], draft);

    expect(calls(ctx, 'fillRect')).toEqual([[0, 0, WHITEBOARD_WIDTH, WHITEBOARD_HEIGHT]]);
    // 2 committed polylines + 1 draft dot.
    expect(calls(ctx, 'stroke')).toHaveLength(2);
    expect(calls(ctx, 'fill')).toHaveLength(1);
    expect(ctx.strokeStyle).toBe('#1971c2');
  });
});

describe('setupCanvasDpi', () => {
  it('sizes the backing store by the device pixel ratio and scales the context', () => {
    const canvas = document.createElement('canvas');
    const ctx = fakeContext() as unknown as CanvasRenderingContext2D;
    vi.spyOn(canvas, 'getContext').mockReturnValue(ctx as never);
    vi.stubGlobal('devicePixelRatio', 2);

    const returned = setupCanvasDpi(canvas);

    expect(returned).toBe(ctx);
    expect(canvas.width).toBe(WHITEBOARD_WIDTH * 2);
    expect(canvas.height).toBe(WHITEBOARD_HEIGHT * 2);
    expect(calls(ctx, 'setTransform')).toEqual([[2, 0, 0, 2, 0, 0]]);
    vi.unstubAllGlobals();
  });

  it('answers null when the browser has no Canvas2D (F3)', () => {
    const canvas = document.createElement('canvas');
    vi.spyOn(canvas, 'getContext').mockReturnValue(null);

    expect(setupCanvasDpi(canvas)).toBeNull();
  });
});

describe('exportPngDataUrl', () => {
  it('renders on an offscreen logical-size canvas and answers a PNG data URL', () => {
    const ctx = fakeContext();
    const realCreate = document.createElement.bind(document);
    const created: HTMLCanvasElement[] = [];
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const element = realCreate(tag);
      if (tag === 'canvas') {
        vi.spyOn(element as HTMLCanvasElement, 'getContext').mockReturnValue(ctx as never);
        (element as HTMLCanvasElement).toDataURL = () => 'data:image/png;base64,QUJD';
        created.push(element as HTMLCanvasElement);
      }
      return element;
    });

    const dataUrl = exportPngDataUrl([stroke()]);

    expect(dataUrl).toBe('data:image/png;base64,QUJD');
    // The export canvas is the LOGICAL size, not the DPR-scaled board.
    expect(created[0]?.width).toBe(WHITEBOARD_WIDTH);
    expect(created[0]?.height).toBe(WHITEBOARD_HEIGHT);
    expect(calls(ctx, 'stroke')).toHaveLength(1);
    vi.restoreAllMocks();
  });
});

describe('normalizePoint', () => {
  it('maps a client position inside the box to 0–1', () => {
    const rect = { left: 100, top: 50, width: 640, height: 400 } as DOMRect;

    expect(normalizePoint(100, 50, rect)).toEqual({ x: 0, y: 0 });
    expect(normalizePoint(420, 250, rect)).toEqual({ x: 0.5, y: 0.5 });
  });

  it('clamps a pointer dragged past the edge', () => {
    const rect = { left: 0, top: 0, width: 100, height: 100 } as DOMRect;

    expect(normalizePoint(180, -20, rect)).toEqual({ x: 1, y: 0 });
  });

  it('answers 0,0 for a zero-sized box (unlaid-out canvas)', () => {
    const rect = { left: 0, top: 0, width: 0, height: 0 } as DOMRect;

    expect(normalizePoint(10, 10, rect)).toEqual({ x: 0, y: 0 });
  });
});
