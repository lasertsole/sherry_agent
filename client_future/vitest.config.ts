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
  },
});
