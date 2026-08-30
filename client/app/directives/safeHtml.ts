import type { DirectiveBinding, ObjectDirective } from 'vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import { chatPurifyConfig } from '@/constants/security';
import { logUtil } from '~/utils/log';

/**
 * v-safe-html — safe rendering directive: markdown → HTML → DOMPurify allowlist
 * sanitization.
 *
 * A replacement for bare `v-html` that funnels "render + sanitize" into a single
 * entry point (paired with the ESLint `vue/no-v-html: error` rule, all raw HTML
 * rendering in the project must go through this directive).
 *
 * The sanitization policy lives in `app/constants/security.ts` (chatPurifyConfig).
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

/** Directive implementation: mounted and updated share the same render path, keeping content up to date during streamed appends */
const render = (el: HTMLElement, binding: DirectiveBinding<string | null | undefined>): void => {
  el.innerHTML = safeMarkdownHtml(binding.value);
};

export const vSafeHtml: ObjectDirective<HTMLElement, string | null | undefined> = {
  mounted: render,
  updated: render
};

export default vSafeHtml;
