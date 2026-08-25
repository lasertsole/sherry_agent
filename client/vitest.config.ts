import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  resolve: {
    alias: {
      // Nuxt-style aliases used throughout the composables
      '~': fileURLToPath(new URL('./app', import.meta.url)),
      '@': fileURLToPath(new URL('./app', import.meta.url)),
      '~~': fileURLToPath(new URL('./', import.meta.url)),
      '@@': fileURLToPath(new URL('./', import.meta.url)),
      // `vue-i18n` is nested inside the Nuxt i18n plugin and is not
      // top-level-resolvable from this workspace. `useSubagentTasks.ts`
      // explicitly imports `useI18n`, so the transform would fail before
      // Vitest's `vi.mock` can intercept. Alias it to a test stub in the
      // unit-test environment only (the app build keeps the real module via
      // Nuxt's own resolver).
      'vue-i18n': fileURLToPath(new URL('./app/composables/__tests__/stubs/vue-i18n.ts', import.meta.url)),
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['app/**/*.{test,spec}.ts'],
    setupFiles: ['app/composables/__tests__/setup.ts'],
    // Composeables rely on `import.meta.env` injected by Vite.
    env: {
      VITE_API_BACK_URL: 'http://localhost:8080',
    },
    deps: {
      optimizer: {
        // Avoid Vite pre-bundling issues w/ Nuxt-adjacent packages (vue only needed)
        include: ['vue'],
      },
    },
    coverage: {
      provider: 'v8', // matches installed @vitest/coverage-v8
      // Default `['text']` prints only the terminal table. `html` emits the
      // interactive (Highcharts) report; without it, the browser view falls
      // back to stale leftover Istanbul-style flat pages with no charts.
      reporter: ['html', 'text'],
    },
  },
});
