import type { Config } from 'dompurify';

/**
 * Unified DOMPurify sanitization configuration — dedicated to chat-message markdown
 * rendering (the v-safe-html directive).
 *
 * Design points:
 *  - Allowlist = the full output tag set of markdown-it core (v15, default preset,
 *    no plugins): block-level (p/blockquote/hr/ul/ol/li/pre/code/headings/tables) +
 *    inline (strong/em/s/del/a/img/br/span).
 *    Syntax highlighting (Shiki v4), diagrams (Mermaid) and math (KaTeX) render
 *    through this same pipeline (2026-10-24): their output tags are enumerated
 *    below and their inline styles survive ONLY inside their own trusted roots
 *    (see TRUSTED_STYLE_ROOTS) — a node outside those roots keeps the original
 *    narrow behavior. Raw-HTML tags outside the allowlist (div/section/video…)
 *    are removed wholesale (their text content is kept) — still intentional.
 *  - Fragment mode: md.render() outputs an HTML fragment, not a full document;
 *    WHOLE_DOCUMENT must never be enabled (it would wrap the output in <html><body>,
 *    breaking fragment semantics).
 *  - The style attribute must be allowed: markdown-it's GFM table alignment is
 *    implemented via style="text-align:…" on th/td. DOMPurify 3.x hard-exempts
 *    style from its URI gate (URI_SAFE_ATTRIBUTES default) and no longer accepts
 *    a URI_SAFE_ATTRIBUTES config override, so the style-value URL gate lives in
 *    the v-safe-html directive's uponSanitizeAttribute hook, judging values
 *    against SAFE_STYLE_VALUE_REGEXP (below): only a whole-value alignment
 *    declaration survives — url()-bearing or unknown declarations are dropped.
 *  - ALLOW_DATA_ATTR: false — data-* attributes in raw HTML are stripped without
 *    exception.
 */
export const chatPurifyConfig: Config = {
  WHOLE_DOCUMENT: false,
  ALLOWED_TAGS: [
    // Block-level
    'p',
    'br',
    'hr',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'blockquote',
    'pre',
    'code',
    'ul',
    'ol',
    'li',
    'table',
    'thead',
    'tbody',
    'tr',
    'th',
    'td',
    // Inline
    'strong',
    'em',
    's',
    'del',
    'span',
    'a',
    'img',
    // Mermaid figure container (the SVG subtree passes the svg profile)
    'figure',
    'figcaption'
  ],
  ALLOWED_ATTR: [
    // a
    'href',
    'title',
    // img
    'src',
    'alt',
    // code (the fence's language-* class), span, etc.
    'class',
    // GFM table alignment (text-align on th/td)
    'style',
    // rel="noopener noreferrer" added to external links by the v-safe-html hook
    'rel',
    // Code-block toolbar acts (delegated click handlers, no real buttons)
    'aria-label'
  ],
  ALLOW_DATA_ATTR: false
};

/**
 * Whole-value gate for style attribute values OUTSIDE any trusted root
 * (enforced by the uponSanitizeAttribute hook in the v-safe-html directive —
 * DOMPurify 3.x hard-exempts style from its own URI gate and provides no
 * config override). A prefix match would admit
 * `text-align:center;background:url(…)`, so only a lone alignment declaration
 * passes: no fetch-capable CSS can ever survive.
 */
export const SAFE_STYLE_VALUE_REGEXP = /^text-align\s*:\s*(?:left|center|right)\s*;?\s*$/i;

/**
 * Trusted style roots: the library-rendered containers whose subtree may carry
 * inline styles. Shiki colors tokens, KaTeX positions struts and Mermaid lays
 * out diagrams — all with enumerable, non-fetch-capable declarations. An AI
 * cannot forge escalation by emitting these classes: the whitelist below never
 * admits `url()`, `position` is limited to relative/absolute (no `fixed`
 * overlay spoofing), and script/onevent attributes are stripped by DOMPurify
 * regardless of scope.
 */
export const TRUSTED_STYLE_ROOTS = '.shiki, .katex, .mermaid-figure';

/** Properties a trusted root's subtree may style (enumerated, never url-bearing). */
export const TRUSTED_STYLE_PROPS: ReadonlySet<string> = new Set([
  'color',
  'background-color',
  'font-style',
  'font-weight',
  'position',
  'top',
  'left',
  'height',
  'min-width',
  'vertical-align',
  'margin-left',
  'padding-right',
  'padding-top',
  'border-radius',
  'text-align'
]);

/** Custom properties a trusted root's subtree may set (Shiki dual themes). */
const TRUSTED_CUSTOM_PROP = /^--[\w-]+\s*:\s*(?:#|rgb|hsl|var\()/i;

/**
 * The style-attribute decision the v-safe-html hook applies, per declaration.
 *
 * 1. the legacy whole-value alignment rule wins first (GFM tables, unchanged);
 * 2. `url(` is rejected unconditionally — even inside a trusted root;
 * 3. inside a trusted root, each declaration is judged alone: allowed property
 *    + no url() keeps that declaration, anything else is dropped from the
 *    attribute while its siblings survive (per-declaration filtering);
 * 4. outside every trusted root the attribute is dropped (the original gate).
 *
 * @param value The raw style attribute value.
 * @param node  The element being sanitized (may live in a detached fragment —
 *   `closest` walks the fragment's parent chain, which is how the trust root
 *   emitted by the renderer itself is found).
 * @returns Whether the attribute may survive.
 */
export function styleValueFilter(value: string | undefined, node: Element | null): string | null {
  if (!value) return null;
  if (SAFE_STYLE_VALUE_REGEXP.test(value.trim())) return value;
  const root = node && typeof node.closest === 'function' ? node.closest(TRUSTED_STYLE_ROOTS) : null;
  if (!root) return null;
  // Per-declaration filtering: a `url(` poisons ONLY its own declaration —
  // its whitelisted siblings survive.
  const kept = value
    .split(';')
    .filter(decl => {
      if (!decl.trim()) return false;
      if (/url\(/i.test(decl)) return false;
      const idx = decl.indexOf(':');
      if (idx < 0) return false;
      const prop = decl.slice(0, idx).trim().toLowerCase();
      if (prop.startsWith('--')) return TRUSTED_CUSTOM_PROP.test(decl.trim());
      return TRUSTED_STYLE_PROPS.has(prop);
    })
    .map(decl => decl.trim());
  return kept.length > 0 ? kept.join(';') : null;
}
