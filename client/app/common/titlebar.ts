/**
 * The custom title bar's shared geometry.
 *
 * The desktop shell disables the OS decorations (`decorations: false`), so the
 * app draws its own 36px bar across the conversation and the right sidebar (and
 * across the full window when the left column is collapsed or the right panel is
 * maximized). Every surface that has to keep clear of it — the home shell's
 * content padding, the right sidebar's maximized overlay and the floating
 * conversation window — reads the height from here instead of hard-coding it.
 *
 * The Web build keeps the browser's own chrome and draws no bar, so callers gate
 * the inset on `isTauri()`.
 *
 * @module common/titlebar
 */

/** Custom title bar height in px (Tailwind `h-9`). */
export const TITLEBAR_HEIGHT = 36;

/** The same height as a CSS length, for inline styles. */
export const TITLEBAR_HEIGHT_CSS = `${TITLEBAR_HEIGHT}px`;
