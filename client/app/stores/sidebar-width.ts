import { defineStore } from 'pinia';
import { ref } from 'vue';

/**
 * Width of the LEFT session sidebar (the shell's first column).
 *
 * Before this store the column was a fixed responsive class — 280px below the
 * `lg` breakpoint, 360px at `lg` — with no way to resize it. The width now
 * lives here so the column can be dragged and the choice survives a reload
 * (the right sidebar has been pointer-resizable for a while; this is the same
 * model, mirrored).
 *
 * The band follows ZCode's workspace shell: a fixed floor and a half-viewport
 * ceiling, so the sidebar can never crowd the conversation out. The floor stays
 * sherry's own 280px (its `md` width) rather than ZCode's 264px — the minimum
 * is the narrowest width the list was ever laid out at.
 */

/** Narrowest allowed: the old `md` width, kept so nothing that fit before now clips. */
export const SIDEBAR_MIN_WIDTH = 280;
/** Ceiling as a fraction of the viewport (ZCode's `WORKSPACE_SIDEBAR_MAX_WIDTH_RATIO`). */
export const SIDEBAR_MAX_WIDTH_RATIO = 0.5;
/** Fallback ceiling when no viewport is known (SSR / a test without a window). */
export const SIDEBAR_FALLBACK_MAX_WIDTH = 720;
/** Width a fresh profile opens at below the `lg` breakpoint. */
export const SIDEBAR_MD_WIDTH = 280;
/** Width a fresh profile opens at `lg` and up — the old responsive default. */
export const SIDEBAR_LG_WIDTH = 360;
/** `lg` in Tailwind terms: the breakpoint the old `lg:w-[360px]` class used. */
export const SIDEBAR_LG_BREAKPOINT = 1024;

/**
 * Default width for a fresh profile: the same choice the responsive classes
 * used to make (360 on a desktop-width window, 280 otherwise).
 * @param viewportWidth Window width in px.
 * @returns The width a fresh profile opens at.
 */
export function defaultSidebarWidth(viewportWidth: number): number {
  return viewportWidth >= SIDEBAR_LG_BREAKPOINT ? SIDEBAR_LG_WIDTH : SIDEBAR_MD_WIDTH;
}

/**
 * Clamp a requested width into the allowed band.
 *
 * `reserved` is the space the rest of the shell needs (the conversation's
 * 320px floor plus the right sidebar's 240px floor when it is open): the
 * ceiling shrinks so a wide drag cannot push those columns past their own
 * minimums and overflow the window. On a viewport too narrow for the band the
 * ceiling wins — a column wider than the window helps nobody, and the
 * auto-collapse composable is closing the side columns there anyway.
 *
 * @param width Requested width in px.
 * @param viewportWidth Current window width (tests pass it explicitly).
 * @param reserved Pixels the other columns need at minimum.
 * @returns The width actually applied (inside the band).
 */
export function clampSidebarWidth(
  width: number,
  viewportWidth: number = typeof window === 'undefined' ? 0 : window.innerWidth,
  reserved: number = 0
): number {
  const ratioCap = viewportWidth > 0 ? Math.floor(viewportWidth * SIDEBAR_MAX_WIDTH_RATIO) : SIDEBAR_FALLBACK_MAX_WIDTH;
  const roomCap = viewportWidth > 0 ? viewportWidth - Math.max(0, reserved) : ratioCap;
  const cap = Math.max(0, Math.min(ratioCap, roomCap));
  const floor = Math.min(SIDEBAR_MIN_WIDTH, cap);
  return Math.round(Math.min(cap, Math.max(floor, width)));
}

/**
 * The shell's left column: its expanded width, persisted across reloads.
 *
 * Collapsed is NOT stored here — the parent owns it (`isSidebarCollapsed`), and
 * a collapsed column is width 0 regardless of this value, so expanding again
 * returns to the width the user last chose.
 */
export const useSidebarWidthStore = defineStore(
  'sidebarWidth',
  () => {
    /** Expanded width in px (clamped on every write; persisted). */
    const width = ref(defaultSidebarWidth(typeof window === 'undefined' ? SIDEBAR_LG_BREAKPOINT : window.innerWidth));

    /**
     * Set the width from a drag (clamped into the allowed band).
     * @param next Requested width in px.
     * @param viewportWidth Current window width.
     * @param reserved Pixels the rest of the shell needs at minimum.
     */
    function setWidth(next: number, viewportWidth?: number, reserved: number = 0): void {
      width.value = clampSidebarWidth(next, viewportWidth, reserved);
    }

    /**
     * Re-clamp the stored width against a shrunken viewport (no-op while it
     * still fits) — mirrors the right sidebar's `fitToViewport`.
     * @param viewportWidth Current window width.
     * @param reserved Pixels the rest of the shell needs at minimum.
     */
    function fitToViewport(viewportWidth: number, reserved: number = 0): void {
      const next = clampSidebarWidth(width.value, viewportWidth, reserved);
      if (next !== width.value) width.value = next;
    }

    return { width, setWidth, fitToViewport };
  },
  {
    persist: {
      pick: ['width']
    }
  }
);
