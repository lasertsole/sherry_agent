/**
 * Mermaid renderer singleton for the markdown pipeline (v12).
 *
 * Renders in the PARENT page with `securityLevel: 'strict'` (mermaid itself
 * neutralizes `%%{init}%%` script injection) — a sandboxed iframe cannot host
 * the library (CDN breaks CSP/offline; bundled deps cannot be shared), and the
 * SVG output is sanitized again by the caller before insertion. The click-to-
 * zoom reuses the global ImagePreviewOverlay with a serialized data URL.
 *
 * Theme follows the color mode AT RENDER TIME (re-initializing mermaid is
 * supported); rendered results are cached per (theme, code) and fail-open.
 *
 * @module composables/use-mermaid
 */

import { logUtil } from '~/utils/log';

type MermaidInstance = (typeof import('mermaid'))['default'];

let mermaidPromise: Promise<MermaidInstance> | null = null;
let initializedTheme = '';
const cache = new Map<string, string>();
let renderCounter = 0;

function currentDarkMode(): boolean {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

async function getMermaid(): Promise<MermaidInstance> {
  const theme = currentDarkMode() ? 'dark' : 'default';
  if (!mermaidPromise || initializedTheme !== theme) {
    initializedTheme = theme;
    mermaidPromise = import('mermaid').then(m => {
      const mm = m.default;
      mm.initialize({ startOnLoad: false, securityLevel: 'strict', theme });
      return mm;
    });
  }
  return mermaidPromise;
}

/**
 * Render one mermaid diagram to an SVG string.
 *
 * @param code The mermaid source (```mermaid fence body).
 * @returns The SVG markup, or null on a parse/render failure (the caller
 *   keeps the plain code block).
 */
export async function renderMermaid(code: string): Promise<string | null> {
  try {
    if (!code.trim()) return null;
    const mm = await getMermaid();
    const theme = initializedTheme;
    const key = `${theme}\u0000${code}`;
    const cached = cache.get(key);
    if (cached) return cached;
    const { svg } = await mm.render(`mmd-${Date.now()}-${renderCounter++}`, code);
    cache.set(key, svg);
    return svg;
  } catch (error) {
    logUtil.w('[use-mermaid] render failed:', error);
    return null;
  }
}

/** Test seam: drop the singleton and cache. */
export function _resetMermaidForTest(): void {
  mermaidPromise = null;
  initializedTheme = '';
  cache.clear();
}
