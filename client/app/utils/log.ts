/**
 * Environment-aware logging utility (Nuxt-adapted version of the minimal implementation
 * from the "error captured factory function" doc, 03-errorCapturedFactoryFunction.md §2.3).
 *
 * - `import.meta.dev` / `import.meta.env.PROD` are statically replaced at build time by Nuxt/Vite.
 *   Key implementation constraint: the expression must be the **literal** `import.meta.dev` or
 *   `import.meta.env.PROD` —
 *   if accessed via an alias such as `const meta = import.meta` and then `meta.dev`, the
 *   aliased property does not exist at runtime (undefined → always falsy) and every
 *   environment check silently fails
 *   (see the similar pitfall note in the comment at the top of composables/toast.ts).
 * - The error level is downgraded to warn in dev (to avoid red console noise); in all other
 *   environments it outputs `console.error` — which is captured by composables/clientLog.ts
 *   and persisted to IndexedDB (viewable in the "Log Viewer → Client" dialog).
 */
export class SimpleLogger {
  /** Normal log: silent in production. */
  l(...args: unknown[]): void {
    if (!import.meta.env.PROD) console.log(...args);
  }

  /** Warning log: silent in production. */
  w(...args: unknown[]): void {
    if (!import.meta.env.PROD) console.warn(...args);
  }

  /** Error log: downgraded to warn in dev; otherwise outputs console.error (goes into the clientLog error bucket). */
  e(...args: unknown[]): void {
    if (import.meta.dev) {
      console.warn('dev环境报错', ...args);
    } else {
      console.error(...args);
    }
  }
}

export const logUtil = new SimpleLogger();
