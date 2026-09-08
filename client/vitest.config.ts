import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

// Single unified Vitest config for the colocated test suites:
//   - unit        : `app/**/__tests__/*.test.ts` (pure composables + logic)
//   - integration : `app/**/__tests__/*.integration.test.ts` (real .vue SFCs)
// Type distinction is by FILE NAME suffix, not by directory or config.
export default defineConfig({
  plugins: [
    // SFC compilation for the integration suite (no-op cost for pure .ts files)
    vue()
  ],
  resolve: {
    alias: {
      // Nuxt-style aliases used throughout the composables
      '~': fileURLToPath(new URL('./app', import.meta.url)),
      '@': fileURLToPath(new URL('./app', import.meta.url)),
      '~~': fileURLToPath(new URL('./', import.meta.url)),
      '@@': fileURLToPath(new URL('./', import.meta.url)),
      // `vue-i18n` is nested inside the Nuxt i18n plugin and is not
      // top-level-resolvable from this workspace. SFCs and `useSubagentTasks.ts`
      // import `{ useI18n } from 'vue-i18n'` explicitly, so the transform would
      // fail before Vitest's `vi.mock` can intercept. Alias both suites to one
      // zh-locale-backed stub: keys resolve against the real zh messages and
      // fall back to the key itself on a miss (identity for unknown keys).
      'vue-i18n': fileURLToPath(new URL('./app/composables/__tests__/stubs/vue-i18n.ts', import.meta.url)),
      // Nuxt's virtual auto-registered-components module (no Nuxt build in Vitest)
      '#components': fileURLToPath(new URL('./app/composables/__tests__/stubs/components.ts', import.meta.url))
    }
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    // Colocated tests only; `*.integration.test.ts` matches this glob too
    include: ['app/**/*.{test,spec}.ts'],
    setupFiles: ['app/composables/__tests__/setup.ts'],
    // Composeables rely on `import.meta.env` injected by Vite.
    env: {
      VITE_API_BACK_URL: 'http://localhost:8080'
    },
    css: false,
    deps: {
      optimizer: {
        // Avoid Vite pre-bundling issues w/ Nuxt-adjacent packages
        include: ['vue', '@vue/test-utils']
      }
    },
    coverage: {
      provider: 'v8', // matches installed @vitest/coverage-v8
      // Default `['text']` prints only the terminal table. `html` emits the
      // interactive (Highcharts) report; without it, the browser view falls
      // back to stale leftover Istanbul-style flat pages with no charts.
      reporter: ['html', 'text']
    }
  }
});
