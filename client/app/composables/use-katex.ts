/**
 * Lazy KaTeX registration for the shared markdown-it singleton.
 *
 * The formula pass MUST happen at the markdown lexer level (a `$` inside a
 * code fence is not a formula — a DOM-level scan would misjudge that), so the
 * plugin joins the `md` singleton in safeHtml.ts via this registration. It is
 * kicked on the first directive mount and is idempotent; renders that happen
 * BEFORE the library lands show `$…$` as plain text (fail-open) — the next
 * re-render (new message, expand-full-text, file viewer) picks formulas up.
 *
 * @module composables/use-katex
 */
import { logUtil } from '~/utils/log';

let registration: Promise<void> | null = null;
let ready = false;

/**
 * Register the KaTeX markdown-it plugin exactly once.
 *
 * @param register The registration callback — the caller owns the markdown-it
 *   singleton, this module only resolves the plugin (the CJS double-default
 *   interop lives here).
 * The directive calls this on every mount; it no-ops after the first. The
 * returned promise settles when the lazy import has finished (success or
 * failure), so callers can re-render the formulas they rendered before it
 * landed.
 */
export function registerKatex(register: (plugin: unknown) => void): Promise<void> {
  if (registration) return registration;
  registration = import('@vscode/markdown-it-katex')
    .then(module => {
      const moduleWithDefault = module as { default?: { default?: unknown } | unknown };
      const plugin =
        moduleWithDefault.default &&
        typeof moduleWithDefault.default === 'object' &&
        'default' in moduleWithDefault.default
          ? (moduleWithDefault.default as { default: unknown }).default
          : moduleWithDefault.default;
      register(plugin);
      ready = true;
    })
    .catch(error => {
      logUtil.w('[use-katex] registration failed; formulas stay plain text:', error);
    });
  return registration;
}

/** Whether the plugin has been registered (formulas render from here on). */
export function isKatexReady(): boolean {
  return ready;
}
