import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import pluginVue from 'eslint-plugin-vue';
import json from '@eslint/json';
import markdown from '@eslint/markdown';
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
      // 禁止直接使用 v-html 插入未净化 HTML，防止 XSS。
      // 唯一例外的 ChatBox.vue 绑定的是 safeHtml()（markdown-it + DOMPurify 净化）后的输出，
      // 已以行级 eslint-disable 注明理由。
      'vue/no-v-html': 'error',
      'vue/multi-word-component-names': 'off',
      'vue/no-mutating-props': 'off'
    }
  },
  {
    rules: {
      'no-console': 'warn',
      'no-undef': 'off', //交给nuxt框架检查
      '@typescript-eslint/no-unsafe-function-type': 'off',
      '@typescript-eslint/no-explicit-any': 'warn',
      semi: ['error']
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
