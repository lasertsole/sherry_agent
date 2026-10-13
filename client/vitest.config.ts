import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import unimport from 'unimport/unplugin';
import { fileURLToPath } from 'node:url';

// Single unified Vitest config for the colocated test suites:
//   - unit        : `app/**/__tests__/*.test.ts` (pure composables + logic)
//   - integration : `app/**/__tests__/*.integration.test.ts` (real .vue SFCs)
// Type distinction is by FILE NAME suffix, not by directory or config.
export default defineConfig({
  plugins: [
    // SFC compilation for the integration suite (no-op cost for pure .ts files)
    vue(),
    // Attach SFC `<i18n lang="json">` block messages onto the compiled
    // component (`__i18n`), mirroring what @intlify/unplugin-vue-i18n does in
    // the real Nuxt build. plugin-vue compiles each i18n custom block to an
    // inert `import blockN from "file.vue?vue&type=i18n..."` (Vite parses the
    // JSON; the block never reaches the component), so without this pass the
    // integration suite cannot resolve component-local keys. The stubbed
    // `vue-i18n` module reads `__i18n` off the current instance and overlays it
    // over the global zh messages (local scope with global fallback).
    {
      name: 'vitest-sfc-i18n-blocks',
      enforce: 'post',
      transform(code, id) {
        const [filename] = id.split('?', 2);
        if (!filename.endsWith('.vue')) return;
        const blockVars = [...code.matchAll(/import (block\d+) from "[^"]+\?vue&type=i18n[^"]*"/g)].map(m => m[1]);
        if (!blockVars.length || !code.includes('_sfc_main')) return;
        return {
          code: `${code}\nif (typeof _sfc_main !== 'undefined') _sfc_main.__i18n = [${blockVars.join(', ')}];\n`,
          map: null
        };
      }
    },
    // Mirror Nuxt's unimport transform for app/composables/ exports (enforced
    // as bare symbols by the `no-restricted-imports` rule in
    // eslint.config.mjs). Without this injection those identifiers are
    // ReferenceErrors under Vitest. unimport stays on the same 6.x major as
    // Nuxt 4.5 so test-time injection matches the production build. Tests are
    // excluded: they run outside the Nuxt pipeline and import explicitly.
    unimport.vite({
      dirs: ['./app/composables'],
      addons: { vueTemplate: true },
      exclude: [/[\\/]node_modules[\\/]/, /[\\/]\.git[\\/]/, /[\\/]__tests__[\\/]/]
    })
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
