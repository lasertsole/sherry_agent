import type { ObjectDirective } from 'vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import { chatPurifyConfig, styleValueFilter } from '@/constants/security';
import { safeT } from '~/utils/i18n';
import { logUtil } from '~/utils/log';

/**
 * v-safe-html — safe rendering directive: markdown → HTML → DOMPurify allowlist
 * sanitization, plus a post-render ENHANCEMENT pass that upgrades code blocks
 * (Shiki), diagrams (Mermaid) and (via a lazy markdown-it plugin) formulas
 * (KaTeX) after the stream has settled.
 *
 * A replacement for bare `v-html` that funnels "render + sanitize" into a single
 * entry point (paired with the ESLint `vue/no-v-html: error` rule, all raw HTML
 * rendering in the project must go through this directive).
 *
 * The sanitization policy lives in `app/constants/security.ts` (chatPurifyConfig);
 * the style-attribute gate (alignment values + the trusted-renderer roots) is
 * `styleValueFilter` in the same file, applied by the uponSanitizeAttribute
 * hook below.
 *
 * Usage:
 *   <div v-safe-html="message.content"></div>
 *
 * Streaming-render notes:
 *   As AI reply chunks are appended, message.content keeps changing, so Vue calls the
 *   `updated` hook to re-render/re-sanitize whenever the bound value changes; when the
 *   bound value is unchanged, no repeated sanitization happens (compared with the
 *   previous approach of calling safeHtml() for every message during render, this
 *   removes pointless repeated computation).
 *
 * Enhancement notes:
 *   The enhancement pass is fire-and-forget and IDEMPOTENT (blocks it already
 *   upgraded carry `data-enhanced`). Callers mark a still-streaming element
 *   with `data-streaming="true"` — the pass returns immediately then, so a
 *   stream pays ZERO enhancement cost and the settled render triggers exactly
 *   one upgrade. Elements without the attribute (file viewer, settled bubbles)
 *   are always eligible.
 */

/** markdown-it instance (module-level singleton): html: true passes raw HTML through (DOMPurify's allowlist gates it afterwards); linkify auto-detects bare links */
const md = new MarkdownIt({ html: true, linkify: true });

// Post-sanitize hook: adds rel="noopener noreferrer" to all <a> elements,
// preventing links opened in a new context from obtaining a window.opener reference
// (reverse tabnabbing).
// afterSanitizeAttributes runs after attribute validation, so what is written here is
// not stripped again.
DOMPurify.addHook('afterSanitizeAttributes', node => {
  const el = node as Element | null;
  if (el && el.tagName === 'A') {
    el.setAttribute('rel', 'noopener noreferrer');
  }
});

// Sanitize-time hook: DOMPurify hard-exempts style from its URI gate and offers
// no config override in 3.x, so the style-value gate is enforced here — the
// decision lives in security.ts (`styleValueFilter`): the legacy alignment rule,
// plus per-declaration whitelisted styles inside the trusted renderer roots
// (.shiki / .katex / .mermaid-figure), with `url(` rejected in every scope.
DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  if (data.attrName === 'style') {
    const filtered = styleValueFilter(data.attrValue, node as Element | null);
    if (filtered === null) {
      data.keepAttr = false;
    } else {
      data.attrValue = filtered;
    }
  }
});
/**
 * Render markdown text into an allowlist-sanitized, safe HTML fragment.
 *
 * @param content Raw markdown text (message body)
 * @returns HTML safe to assign to innerHTML; returns an empty string when the input
 *   is empty or sanitization fails (fail-safe)
 */
export function safeMarkdownHtml(content: string | null | undefined): string {
  if (!content) return '';
  try {
    return DOMPurify.sanitize(md.render(content), chatPurifyConfig);
  } catch (error) {
    // Any exception in the sanitization step is treated as "untrusted": better to
    // drop the body than output unsanitized content
    logUtil.e('[v-safe-html] sanitize failed, fallback to empty html:', error);
    return '';
  }
}

/**
 * Sanitize an already-HTML fragment (no markdown pass).
 *
 * The docx viewer's use case: mammoth hands back real HTML, and running it
 * through markdown-it first would re-parse (and possibly re-arrange) block
 * structures like tables. Same allowlist, same fail-safe.
 *
 * @param html An HTML fragment produced by a trusted local converter (its
 *   origin is still untrusted content, so the allowlist gate is mandatory)
 * @returns HTML safe to assign to innerHTML; empty on failure (fail-safe)
 */
export function safeHtmlOnly(html: string | null | undefined): string {
  if (!html) return '';
  try {
    return DOMPurify.sanitize(html, chatPurifyConfig);
  } catch (error) {
    logUtil.e('[v-safe-html] sanitize failed, fallback to empty html:', error);
    return '';
  }
}

// ── Enhancement pass: Shiki code blocks + Mermaid diagrams ────────────────
// DOM-level and idempotent: it walks `pre > code[class*="language-"]` blocks
// that carry no `data-enhanced`, replaces each with its rendered form, and
// marks it either way — so any number of passes costs only the untouched
// blocks. The click acts (copy / wrap / zoom) are ONE delegated listener per
// directive element, not mounted Vue components.

/**
 * The sanitizer for renderer output — same allowlist, trusted roots scoped in security.ts.
 * @param html
 */
function sanitizeEnhanced(html: string): string {
  return DOMPurify.sanitize(html, chatPurifyConfig);
}

/** The SVG profile for Mermaid output: enumerated svg elements, no HTML mixed in. */
const MERMAID_PURIFY_CONFIG = { USE_PROFILES: { svg: true, svgFilters: true }, ADD_ATTR: ['id', 'aria-label'] };

function escapeHtml(text: string): string {
  return text.replace(
    /[&<>"']/g,
    ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch] ?? ch
  );
}

/**
 * Build the replacement for one code block: a shiki <pre> with the toolbar row.
 * @param highlighted
 * @param lang
 */
function buildShikiBlock(highlighted: string, lang: string): HTMLElement | null {
  const holder = document.createElement('div');
  holder.innerHTML = sanitizeEnhanced(highlighted);
  const pre = holder.firstElementChild as HTMLElement | null;
  if (!pre || pre.tagName !== 'PRE') return null;
  const bar = document.createElement('span');
  bar.className = 'code-toolbar';
  bar.innerHTML =
    `<span class="code-lang">${escapeHtml(lang)}</span>` +
    `<span class="code-act" data-act="copy" aria-label="${escapeHtml(safeT('chatBox.copy'))}">${escapeHtml(safeT('chatBox.copy'))}</span>` +
    `<span class="code-act" data-act="wrap" aria-label="${escapeHtml(safeT('chatBox.codeWrap'))}">${escapeHtml(safeT('chatBox.codeWrap'))}</span>`;
  pre.appendChild(bar);
  return pre;
}

/**
 * Build the replacement for one mermaid block: a sanitized SVG figure + zoom act.
 * @param svg
 */
function buildMermaidFigure(svg: string): HTMLElement | null {
  const holder = document.createElement('div');
  holder.innerHTML = DOMPurify.sanitize(svg, MERMAID_PURIFY_CONFIG);
  const inner = holder.querySelector('svg');
  if (!inner) return null;
  const figure = document.createElement('figure');
  figure.className = 'mermaid-figure';
  figure.appendChild(inner);
  const cap = document.createElement('figcaption');
  cap.className = 'code-act';
  cap.dataset.act = 'zoom';
  const zoomLabel = safeT('chatBox.codeZoom');
  cap.setAttribute('aria-label', zoomLabel);
  cap.textContent = zoomLabel;
  figure.appendChild(cap);
  return figure;
}

/**
 * The toolbar copy act must read CODE text only — the toolbar labels live inside the pre.
 * @param pre
 */
function codeTextOf(pre: Element): string {
  return pre.querySelector('code')?.textContent ?? pre.textContent ?? '';
}

/**
 * Make rendered formulas keyboard/click zoomable.
 *
 * The mark is added AFTER sanitization (data-* attributes in raw HTML are
 * stripped), and only for the markdown path — the formula preview itself uses
 * `.fragment` and must not open another preview.
 * @param el The rendered markdown element.
 */
function markFormulaZoom(el: HTMLElement): void {
  const label = safeT('chatBox.codeZoom');
  for (const formula of el.querySelectorAll<HTMLElement>('.katex')) {
    // A formula inside a link/button keeps that element's native action.
    if (formula.closest('a, button')) continue;
    formula.dataset.act = 'zoom';
    formula.setAttribute('role', 'button');
    formula.setAttribute('tabindex', '0');
    formula.setAttribute('aria-label', label);
    formula.classList.add('katex-zoom');
  }
}

/**
 * Upgrade one directive element's eligible blocks (async, fire-and-forget).
 * Skipped entirely while the element is marked `data-streaming="true"`.
 * @param el The element whose innerHTML was just rendered.
 */
async function enhance(el: HTMLElement): Promise<void> {
  if (el.dataset.streaming === 'true') return;
  const blocks = [...el.querySelectorAll('pre > code[class*="language-"]')].filter(
    code => !code.hasAttribute('data-enhanced')
  );
  for (const code of blocks.slice(0, 30)) {
    code.setAttribute('data-enhanced', 'true'); // claimed before the await: a re-pass must not double-handle
    const lang = /language-([\w-]+)/.exec(code.className)?.[1]?.toLowerCase() ?? '';
    try {
      if (lang === 'mermaid') {
        const { renderMermaid } = await import('~/composables/use-mermaid');
        const svg = await renderMermaid(code.textContent ?? '');
        if (!svg) continue;
        const figure = buildMermaidFigure(svg);
        if (figure) code.parentElement?.replaceWith(figure);
        continue;
      }
      const { highlightCode } = await import('~/composables/use-shiki');
      const highlighted = await highlightCode(code.textContent ?? '', lang);
      if (!highlighted) continue;
      const pre = buildShikiBlock(highlighted, lang);
      if (pre) code.parentElement?.replaceWith(pre);
    } catch (error) {
      // fail-open: the original plain block stays
      logUtil.w('[v-safe-html] enhance failed for block:', lang, error);
    }
  }
}

/**
 * One delegated click handler per directive element (copy / wrap / zoom).
 * @param event
 */
function delegatedAct(event: Event): void {
  const target = (event.target as HTMLElement | null)?.closest('[data-act]') as HTMLElement | null;
  if (!target) return;
  const act = target.dataset.act;
  if (act === 'copy') {
    const pre = target.closest('pre');
    if (!pre) return;
    void navigator.clipboard?.writeText(codeTextOf(pre)).then(() => {
      const previous = target.textContent;
      target.textContent = safeT('chatBox.copied');
      setTimeout(() => {
        target.textContent = previous ?? safeT('chatBox.copy');
      }, 2000);
    });
  } else if (act === 'wrap') {
    target.closest('pre')?.classList.toggle('code-wrap');
  } else if (act === 'zoom') {
    const formula = target.closest('.katex');
    if (formula) {
      // Clone the display wrapper when there is one (block formulas keep their
      // centering), then drop the chat-only zoom affordances before the overlay
      // sanitizes the fragment again.
      const source = formula.closest('.katex-display') ?? formula;
      const clone = source.cloneNode(true) as HTMLElement;
      for (const node of [clone, ...clone.querySelectorAll('*')]) {
        node.removeAttribute('data-act');
        node.removeAttribute('role');
        node.removeAttribute('tabindex');
        node.removeAttribute('aria-label');
        node.classList.remove('katex-zoom');
      }
      void import('~/composables/use-formula-preview').then(({ useFormulaPreview }) => {
        useFormulaPreview().openFormulaPreview(clone.outerHTML);
      });
      return;
    }
    const svg = target.closest('figure')?.querySelector('svg');
    if (!svg) return;
    const dataUrl = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(new XMLSerializer().serializeToString(svg))))}`;
    void import('~/composables/useImagePreview').then(({ useImagePreview }) => {
      useImagePreview().openPreview(dataUrl);
    });
  }
}

/** Per-element click handlers, so `unmounted` can remove exactly what it added. */
const delegatedHandlers = new WeakMap<HTMLElement, (event: Event) => void>();
/** Per-element keyboard handlers for the focusable formula zoom controls. */
const delegatedKeyHandlers = new WeakMap<HTMLElement, (event: KeyboardEvent) => void>();

/**
 * Enter/Space on a focused formula mirrors its click-to-zoom.
 * @param event
 */
function delegatedKey(event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const target = (event.target as HTMLElement | null)?.closest('.katex[data-act="zoom"]') as HTMLElement | null;
  if (!target) return;
  event.preventDefault();
  target.click();
}

/** Last rendered input per element: lets `updated` skip a no-op rewrite. */
interface SafeHtmlRenderState {
  value: string | null | undefined;
  fragment: boolean;
  streaming: boolean;
}

const renderStates = new WeakMap<HTMLElement, SafeHtmlRenderState>();

/**
 * Directive implementation: mounted and updated share the same render path, keeping content up to date during streamed appends.
 *
 * The `.fragment` modifier skips the markdown pass (the input is already HTML
 * — mammoth's docx output); the default path renders markdown. Both paths
 * schedule the enhancement pass afterwards (which itself no-ops while the
 * element is marked streaming).
 * @param el
 * @param value Rendered markdown/HTML source.
 * @param fragment Whether the input is already HTML (the `.fragment` path).
 */
const render = (el: HTMLElement, value: string | null | undefined, fragment: boolean): void => {
  el.innerHTML = fragment ? safeHtmlOnly(value) : safeMarkdownHtml(value);
  if (!fragment) markFormulaZoom(el);
  renderStates.set(el, {
    value,
    fragment,
    streaming: el.dataset.streaming === 'true'
  });
  void enhance(el);
};

/**
 * Re-render once KaTeX's lazy registration lands.
 *
 * Registration is fire-and-forget, so the first render of a settled message
 * happens before `$…$` can be parsed. Without this pass formulas stayed raw
 * until some unrelated update (a scroll-driven row update used to be that
 * update) re-rendered the bubble.
 * @param el
 */
function rerenderWhenKatexReady(el: HTMLElement): void {
  void import('~/composables/use-katex').then(async m => {
    if (m.isKatexReady()) return;
    await m.registerKatex(plugin => md.use(plugin as never));
    if (!m.isKatexReady() || !el.isConnected) return;
    const state = renderStates.get(el);
    if (!state || state.fragment) return;
    render(el, state.value, false);
  });
}

export const vSafeHtml: ObjectDirective<HTMLElement, string | null | undefined> = {
  mounted(el, binding) {
    const fragment = Boolean(binding.modifiers && 'fragment' in binding.modifiers);
    render(el, binding.value, fragment);
    const handler = delegatedAct.bind(null);
    delegatedHandlers.set(el, handler);
    el.addEventListener('click', handler);
    const keyHandler = delegatedKey.bind(null);
    delegatedKeyHandlers.set(el, keyHandler);
    el.addEventListener('keydown', keyHandler);
    if (!fragment) rerenderWhenKatexReady(el);
  },
  updated(el, binding) {
    const fragment = Boolean(binding.modifiers && 'fragment' in binding.modifiers);
    const state = renderStates.get(el);
    const streaming = el.dataset.streaming === 'true';
    // A virtual list re-renders its window on every scroll. Rewriting
    // innerHTML there destroyed the enhanced (Shiki/Mermaid) nodes and made
    // the bubble visibly flash; the same value only needs the streaming-gate
    // bookkeeping below.
    if (!state || state.value !== binding.value || state.fragment !== fragment) {
      render(el, binding.value, fragment);
      return;
    }
    if (state.streaming !== streaming) {
      state.streaming = streaming;
      if (!streaming) void enhance(el);
    }
  },
  unmounted(el) {
    const handler = delegatedHandlers.get(el);
    if (handler) el.removeEventListener('click', handler);
    delegatedHandlers.delete(el);
    const keyHandler = delegatedKeyHandlers.get(el);
    if (keyHandler) el.removeEventListener('keydown', keyHandler);
    delegatedKeyHandlers.delete(el);
    renderStates.delete(el);
  }
};
