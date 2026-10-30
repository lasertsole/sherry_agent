/**
 * The whiteboard's Canvas2D core: strokes, redraw and PNG export.
 *
 * Deliberately dependency-free (no fabric/konva/excalidraw): a stroke is a
 * polyline of NORMALIZED points (0–1 against the fixed logical canvas), so the
 * drawing survives any CSS size without a ResizeObserver and re-exports at a
 * stable resolution. Erasing is drawing with the background colour — the same
 * cheap trick the reference implementation uses; the canvas is repainted from
 * the stroke list on every change, so an undo "un-erases" correctly.
 *
 * @module composables/use-whiteboard-canvas
 */

/** Logical canvas size (all stored points are normalized against it). */
export const WHITEBOARD_WIDTH = 1280;
export const WHITEBOARD_HEIGHT = 800;

/** Fixed palette — the six tones the reference board ships with. */
export const WHITEBOARD_COLORS = ['#1f1f1f', '#e03131', '#1971c2', '#2f9e44', '#f08c00', '#9c36b5'] as const;

/** Stroke width range (px at the logical size). */
export const WHITEBOARD_MIN_WIDTH = 2;
export const WHITEBOARD_MAX_WIDTH = 18;

/** Paper colour; also the eraser's ink (erasing = painting the paper). */
export const WHITEBOARD_BACKGROUND = '#ffffff';

/** The two tools this version supports. */
export type WhiteboardTool = 'pen' | 'eraser';

/** One point, normalized to the logical canvas (0–1 on both axes). */
export interface WhiteboardPoint {
  x: number;
  y: number;
}

/** One continuous mark from pointerdown to pointerup. */
export interface WhiteboardStroke {
  tool: WhiteboardTool;
  /** Hex colour; ignored by the eraser. */
  color: string;
  /** Logical width (WHITEBOARD_MIN_WIDTH..WHITEBOARD_MAX_WIDTH). */
  width: number;
  points: WhiteboardPoint[];
}

/**
 * Convert one normalized point to logical-canvas pixels.
 * @param point Normalized point.
 * @returns `[x, y]` in logical pixels.
 */
function toPixel(point: WhiteboardPoint): [number, number] {
  return [point.x * WHITEBOARD_WIDTH, point.y * WHITEBOARD_HEIGHT];
}

/**
 * Paint one stroke.
 *
 * A single-point stroke is a dot (`arc` + `fill`); anything longer is a round
 * capped/joined polyline. The caller owns `beginPath`-level state — this starts
 * its own path so strokes never leak into each other.
 * @param ctx Target context.
 * @param stroke Stroke to paint.
 * @param backgroundColor Paper colour (the eraser paints with it).
 */
export function drawStroke(
  ctx: CanvasRenderingContext2D,
  stroke: WhiteboardStroke,
  backgroundColor: string = WHITEBOARD_BACKGROUND
): void {
  if (stroke.points.length === 0) return;
  const ink = stroke.tool === 'eraser' ? backgroundColor : stroke.color;
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = stroke.width;
  ctx.strokeStyle = ink;
  ctx.fillStyle = ink;

  if (stroke.points.length === 1) {
    const [x, y] = toPixel(stroke.points[0]!);
    ctx.beginPath();
    ctx.arc(x, y, Math.max(0.5, stroke.width / 2), 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    return;
  }

  ctx.beginPath();
  const [x0, y0] = toPixel(stroke.points[0]!);
  ctx.moveTo(x0, y0);
  for (let index = 1; index < stroke.points.length; index += 1) {
    const [x, y] = toPixel(stroke.points[index]!);
    ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}

/**
 * Repaint the whole board: paper, every committed stroke, then the live draft.
 * @param ctx Target context.
 * @param strokes Committed strokes, oldest first.
 * @param draft The stroke currently being drawn (not yet committed).
 * @param backgroundColor Paper colour.
 */
export function drawDocument(
  ctx: CanvasRenderingContext2D,
  strokes: readonly WhiteboardStroke[],
  draft?: WhiteboardStroke | null,
  backgroundColor: string = WHITEBOARD_BACKGROUND
): void {
  ctx.save();
  ctx.fillStyle = backgroundColor;
  ctx.fillRect(0, 0, WHITEBOARD_WIDTH, WHITEBOARD_HEIGHT);
  ctx.restore();
  for (const stroke of strokes) drawStroke(ctx, stroke, backgroundColor);
  if (draft) drawStroke(ctx, draft, backgroundColor);
}

/**
 * Prepare a canvas for high-DPI drawing.
 *
 * The backing store is the logical size times `devicePixelRatio`, and the
 * context is scaled so drawing code keeps using logical coordinates. The CSS
 * size is deliberately NOT set here — the component owns layout (a `h-full
 * w-full` canvas inside an aspect-ratio box).
 * @param canvas Canvas element.
 * @returns The 2D context, or null when this browser has no Canvas2D (F3).
 */
export function setupCanvasDpi(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(WHITEBOARD_WIDTH * dpr);
  canvas.height = Math.round(WHITEBOARD_HEIGHT * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

/**
 * Render the board to a PNG data URL at the logical resolution.
 *
 * Drawn on an offscreen canvas so the visible board's DPI transform and any
 * draft stroke are irrelevant: the export is always the same 1280×800 image.
 * @param strokes Committed strokes.
 * @param backgroundColor Paper colour.
 * @returns `data:image/png;base64,...`, or null when Canvas2D is unavailable.
 */
export function exportPngDataUrl(
  strokes: readonly WhiteboardStroke[],
  backgroundColor: string = WHITEBOARD_BACKGROUND
): string | null {
  const canvas = document.createElement('canvas');
  canvas.width = WHITEBOARD_WIDTH;
  canvas.height = WHITEBOARD_HEIGHT;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  drawDocument(ctx, strokes, null, backgroundColor);
  return canvas.toDataURL('image/png');
}

/**
 * Normalize a pointer position against the canvas's CSS box.
 * @param clientX Pointer client X.
 * @param clientY Pointer client Y.
 * @param rect The canvas's bounding box.
 * @returns A point clamped to 0–1 (a pointer dragged past the edge keeps drawing
 *   at the edge instead of producing off-canvas coordinates).
 */
export function normalizePoint(clientX: number, clientY: number, rect: DOMRect): WhiteboardPoint {
  const x = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
  const y = rect.height > 0 ? (clientY - rect.top) / rect.height : 0;
  return {
    x: Math.min(1, Math.max(0, x)),
    y: Math.min(1, Math.max(0, y))
  };
}
