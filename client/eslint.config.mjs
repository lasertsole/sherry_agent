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
  pluginVue.configs['flat/essential'],
  { files: ['**/*.vue'], languageOptions: { parserOptions: { parser: tseslint.parser } } },
  { files: ['**/*.json'], plugins: { json }, language: 'json/json', extends: ['json/recommended'] },
  { files: ['**/*.jsonc'], plugins: { json }, language: 'json/jsonc', extends: ['json/recommended'] },
  { files: ['**/*.json5'], plugins: { json }, language: 'json/json5', extends: ['json/recommended'] },
  { files: ['**/*.md'], plugins: { markdown }, language: 'markdown/commonmark', extends: ['markdown/recommended'] },
  {
    ignores: [
      'node_modules',
      'dist',
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
    rules: {
      'no-console': 'warn',
      'vue/multi-word-component-names': 'off',
      'no-undef': 'off', //交给nuxt框架检查
      '@typescript-eslint/no-unsafe-function-type': 'off',
      '@typescript-eslint/no-explicit-any': 'warn',
      'vue/no-mutating-props': 'off',
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
  }
]);
