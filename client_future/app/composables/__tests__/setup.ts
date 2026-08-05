/**
 * Vitest setup for composables tests.
 *
 * Stubs Nuxt auto-imports / runtime globals that are not available in a bare
 * happy-dom environment, so the production composables can be exercised
 * directly without mounting the full Nuxt SPA.
 */
import { vi } from 'vitest';

// `requestApi.ts` calls Nuxt's auto-imported `useFetch(request, opts)` and
// expects it to return `{ data: Ref<Response> }`. Tests can override this
// global per-file via `vi.stubGlobal('useFetch', mock)`.
(globalThis as any).useFetch = vi.fn().mockReturnValue({
  data: { value: null },
  error: { value: null },
});

// Composables under `app/composables/` (messages, workspace, bridge, ...)
// call `fetchApi` as a *Nuxt auto-import* (there is no explicit import in the
// source files), so it is NOT available in a bare Vitest environment. Tests
// stub this global per-file via `vi.stubGlobal('fetchApi', mock)`. Without it
// `import { fetchApi } from '../requestApi'` would fail for the explicit
// importer (requestApi.test.ts) — so keep that explicit import path working
// by leaving `import ... from '../requestApi'` inside test files untouched.
(globalThis as any).fetchApi = vi.fn(() =>
  Promise.resolve({ code: 200, data: null }),
);
