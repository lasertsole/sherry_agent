import type { Config } from 'dompurify';

/**
 * Unified DOMPurify sanitization configuration — dedicated to chat-message markdown
 * rendering (the v-safe-html directive).
 *
 * Design points:
 *  - Allowlist = the full output tag set of markdown-it core (v15, default preset,
 *    no plugins): block-level (p/blockquote/hr/ul/ol/li/pre/code/headings/tables) +
 *    inline (strong/em/s/del/a/img/br/span).
 *    This project does not enable syntax highlighting (shiki/hljs), math (KaTeX), or
 *    task-list plugins, so no tags need to be allowed for them; raw-HTML tags outside
 *    the allowlist (div/section/video…) are removed wholesale (their text content is
 *    kept) — this narrowing is intentional.
 *  - Fragment mode: md.render() outputs an HTML fragment, not a full document;
 *    WHOLE_DOCUMENT must never be enabled (it would wrap the output in <html><body>,
 *    breaking fragment semantics).
 *  - The style attribute must be allowed: markdown-it's GFM table alignment is
 *    implemented via style="text-align:…" on th/td; the attribute value itself is
 *    still sanitized by DOMPurify.
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
    'img'
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
    'rel'
  ],
  ALLOW_DATA_ATTR: false
};
