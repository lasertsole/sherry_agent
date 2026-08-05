import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

// Integration test config.
//
// Mounts REAL .vue SFC components (leaf components + the /home page) with their
// real Vue composition logic, while keeping the backend mocked. This is a
// separate config from the composables unit suite (vitest.config.ts) so the two
// never interfere:
//   - unit suite  : pure `.ts` composable logic (no SFC compiler needed)
//   - integration : `.vue` SFC compilation via @vitejs/plugin-vue
//
// Nuxt auto-imports (useColorMode, fetchApi, get_history_by_page, ...) are not
// available in a bare Vitest/happy-dom environment, so tests stub them on
// `globalThis` (the same pattern already proven in `setup.ts`).
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // Nuxt-style aliases used throughout the app sources
      '~': fileURLToPath(new URL('./app', import.meta.url)),
      '@': fileURLToPath(new URL('./app', import.meta.url)),
      '~~': fileURLToPath(new URL('./', import.meta.url)),
      '@@': fileURLToPath(new URL('./', import.meta.url)),
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['tests/integration/**/*.{test,spec}.ts'],
    setupFiles: ['tests/integration/setup.ts'],
    // `messages.ts` reads `import.meta.env.VITE_API_BACK_URL`.
    env: {
      VITE_API_BACK_URL: 'http://localhost:8080',
    },
    css: false,
    deps: {
      optimizer: {
        include: ['vue', '@vue/test-utils'],
      },
    },
  },
});
