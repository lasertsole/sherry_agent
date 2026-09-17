/**
 * Shared client-environment guard.
 *
 * Several infrastructure modules (`toast.ts`, the connection store, ...) must be safe
 * no-ops when `import.meta.client === false`. The flag lives here as a single
 * module-level value so every consumer shares one test override instead of each
 * module keeping a private copy.
 *
 * Key implementation constraint: production code must reference the **literal**
 * `import.meta.client` — Nuxt/Vite's build-time static replacement only applies to
 * that literal expression. If accessed through an alias such as `const meta = import.meta`
 * then `meta.client`, the aliased property does not exist at runtime (undefined → always
 * falsy) and every client guard silently breaks.
 */

let clientFlagOverride: boolean | null = null;

/**
 * Test-only: explicitly override the client-semantics flag (never call from production code).
 * Background: Vitest's `import.meta` lacks Nuxt's client/server semantics (undefined → falsy),
 * so tests must inject it explicitly.
 * @param client
 */
export function _setClientFlag(client: boolean): void {
  clientFlagOverride = client;
}

/** Whether we are currently in a browser client environment (production uses build-time static replacement; tests use the explicit override). */
export function isClient(): boolean {
  if (clientFlagOverride !== null) return clientFlagOverride;
  return import.meta.client === true;
}
