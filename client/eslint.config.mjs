import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import pluginVue from 'eslint-plugin-vue';
import json from '@eslint/json';
import markdown from '@eslint/markdown';
import jsdoc from 'eslint-plugin-jsdoc';
import { defineConfig } from 'eslint/config';
import eslintPluginPrettier from 'eslint-plugin-prettier';

export default defineConfig([
  {
    files: ['**/*.{js,mjs,cjs,ts,mts,cts,vue}'],
    plugins: { js },
    extends: ['js/recommended'],
    languageOptions: { globals: globals.browser }
  },
  tseslint.configs.recommended,
  // Scope every vue plugin config (parser setup + rules) to .vue files only.
  // Without this, vue rules load on non-SFC files (e.g. package.json using the json language)
  // and crash with "Cannot read properties of undefined (reading 'getDocumentFragment')".
  ...pluginVue.configs['flat/essential'].map(config => ({ ...config, files: ['**/*.vue'] })),
  { files: ['**/*.vue'], languageOptions: { parserOptions: { parser: tseslint.parser } } },
  { files: ['**/*.json'], plugins: { json }, language: 'json/json', extends: ['json/recommended'] },
  { files: ['**/*.jsonc'], plugins: { json }, language: 'json/jsonc', extends: ['json/recommended'] },
  { files: ['**/*.json5'], plugins: { json }, language: 'json/json5', extends: ['json/recommended'] },
  { files: ['**/*.md'], plugins: { markdown }, language: 'markdown/commonmark', extends: ['markdown/recommended'] },
  {
    ignores: [
      'node_modules',
      'dist',
      // Build/generated output & test artifacts (Nuxt build, Tauri build, Playwright/Vitest output)
      '.nuxt',
      '.output',
      '.data',
      'test-results',
      'playwright-report',
      'src-tauri/target',
      'src-tauri/gen',
      'coverage',
      '.git',
      '.husky',
      '.vscode',
      '.idea',
      '.cache',
      '*.min.*',
      '*.config.*',
      '*.lock',
      '*.svg',
      '*.webp',
      '*.gif',
      '*.png',
      '*.jpg',
      '*.jpeg',
      '*.ico',
      '*.toml',
      '*.txt'
    ]
  },
  {
    files: ['**/*.vue'],
    rules: {
      // Forbid inserting unsanitized HTML directly via v-html to prevent XSS.
      // The sole exception is ChatBox.vue, which binds the output of safeHtml()
      // (sanitized by markdown-it + DOMPurify), with the reason noted via a line-level eslint-disable.
      'vue/no-v-html': 'error',
      'vue/multi-word-component-names': 'off',
      'vue/no-mutating-props': 'off'
    }
  },
  {
    // JSDoc signature validation (part3 item 12): comments must stay bound
    // to the code they describe. Only functions that ALREADY carry a JSDoc
    // block are checked; writing new JSDoc is not forced.
    files: ['**/*.{ts,vue}'],
    plugins: { jsdoc },
    settings: { jsdoc: { mode: 'typescript' } },
    rules: {
      // @param names must match the function signature
      'jsdoc/check-param-names': 'error',
      // @returns type must match the actual return type
      'jsdoc/check-types': 'warn',
      // must not describe parameters that do not exist
      'jsdoc/no-undefined-types': 'error',
      // JSDoc blocks must carry description text
      'jsdoc/require-description': 'warn',
      // completeness signals only (warn): the corpus keeps short JSDoc blocks;
      // signature BINDING is enforced by check-param-names above
      'jsdoc/require-param': 'warn',
      // functions WITH a JSDoc block must declare the return (if any)
      'jsdoc/require-returns': 'warn'
    }
  },
  {
    // Naming conventions (part2 item 9): block backend-style snake_case bleed.    // property/method selectors are intentionally NOT enforced — object literals,
    // i18n keys and API payloads are data, not code style.
    // variable keeps PascalCase (Vue component imports) and UPPER_CASE (constants).
    files: ['**/*.{ts,tsx,vue}'],
    rules: {
      '@typescript-eslint/naming-convention': [
        'error',
        { selector: 'function', format: ['camelCase'], leadingUnderscore: 'allow' },
        {
          selector: 'variable',
          format: ['camelCase', 'UPPER_CASE', 'PascalCase'],
          leadingUnderscore: 'allow'
        },
        { selector: 'parameter', format: ['camelCase'], leadingUnderscore: 'allow' },
        { selector: 'class', format: ['PascalCase'] },
        // UPPER_CASE allowed for constant-namespace enums (legacy CHAT_ROLE); lowercase
        // snake_case typeLike names remain blocked
        { selector: 'typeLike', format: ['PascalCase', 'UPPER_CASE'] }
      ]
    }
  },
  {
    // API-contract mirror modules: exported function/parameter names deliberately
    // mirror backend endpoint names (/get_history_by_turn_page, /system_prompt),
    // so snake_case here is a contract, not style bleed.
    files: ['**/composables/messages.ts', '**/composables/workspace.ts'],
    rules: {
      '@typescript-eslint/naming-convention': 'off'
    }
  },
  {
    // Nuxt 4 auto-imports every export of app/composables/ (unimport scans the
    // directory; the generated declarations live in .nuxt/imports.d.ts), so an
    // explicit import of a composable module is redundant. Ban it and use the
    // auto-imported symbol directly. Type-only imports stay legal (types are
    // NOT injected at runtime), and test files are exempt: they run under bare
    // Vitest outside the Nuxt/unimport pipeline and must import explicitly.
    files: ['app/**/*.{ts,mts,cts,vue}'],
    ignores: ['**/__tests__/**', 'app/composables/*.ts'],
    rules: {
      '@typescript-eslint/no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/composables', '@/composables/**', '~/composables', '~/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['~~/composables', '~~/composables/**', '@@/composables', '@@/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: [
                '../composables',
                '../composables/**',
                '../../composables',
                '../../composables/**',
                '../../../composables',
                '../../../composables/**'
              ],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            }
          ]
        }
      ]
    }
  },
  {
    // Composables importing sibling composables use relative paths ('./db')
    // that no alias/parent pattern above can match; inside app/composables/*.ts
    // every relative runtime import IS a composable-to-composable import, so
    // ban those too (this block restates the alias patterns because a later
    // matching flat-config block replaces, not merges, the same rule).
    files: ['app/composables/*.ts'],
    ignores: ['**/__tests__/**'],
    rules: {
      '@typescript-eslint/no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/composables', '@/composables/**', '~/composables', '~/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['~~/composables', '~~/composables/**', '@@/composables', '@@/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['../composables', '../composables/**', '../../composables', '../../composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['./**', '../**'],
              message:
                'Composables are auto-imported; relative sibling imports of composables are banned (type-only imports are allowed).',
              allowTypeImports: true
            }
          ]
        }
      ]
    }
  },
  {
    // bridge.ts facade exception: bridge.ts re-exports the split bridge/* domain
    // modules. Those submodules are NOT auto-imported (Nuxt scans only the top
    // level of composables/), so the facade must reference them relatively.
    // Only relative paths inside the facade's own subtree become legal here;
    // aliases and parent imports stay banned.
    files: ['app/composables/bridge.ts'],
    rules: {
      '@typescript-eslint/no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@/composables', '@/composables/**', '~/composables', '~/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['~~/composables', '~~/composables/**', '@@/composables', '@@/composables/**'],
              message:
                'Nuxt auto-imports app/composables exports; use the symbol directly (type-only imports are allowed).',
              allowTypeImports: true
            },
            {
              group: ['../composables', '../composables/**', '../../composables', '../../composables/**', '../**'],
              message:
                'Composables are auto-imported; only ./bridge/* relative imports are allowed in the bridge facade (type-only imports are allowed).',
              allowTypeImports: true
            }
          ]
        }
      ]
    }
  },
  {
    // Global baseline. MUST stay ABOVE the override block below (flat config:
    // last matching block wins) or the no-console exemptions get overridden.
    rules: {
      'no-console': 'error', // item 17: console.* must go through the log tooling
      'no-undef': 'off', // delegated to the nuxt framework's checks
      '@typescript-eslint/no-unsafe-function-type': 'off',
      '@typescript-eslint/no-explicit-any': 'warn',
      semi: ['error']
    }
  },
  {
    // Log infrastructure owns console calls; tests legitimately mock/console.
    files: ['**/__tests__/**', '**/clientLog.ts'],
    rules: {
      'no-console': 'off',
      '@typescript-eslint/no-explicit-any': 'off'
    }
  },
  {
    plugins: {
      prettier: eslintPluginPrettier
    },
    rules: {
      'prettier/prettier': 'error'
    }
  },
  // eslint-plugin-prettier cannot parse markdown through the @eslint/markdown language plugin
  // (every .md fails with "Parsing error: Unexpected token"). Markdown stays linted by the
  // markdown/* rules; format it via the prettier CLI instead.
  { files: ['**/*.md'], rules: { 'prettier/prettier': 'off' } },
  // tsconfig.json is Nuxt-generated and contains JSONC comments; parse it with the jsonc language.
  { files: ['tsconfig.json'], language: 'json/jsonc' }
]);
