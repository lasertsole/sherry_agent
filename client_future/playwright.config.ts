import { defineConfig, devices } from '@playwright/test';

/**
 * Repro config — single browser (installed MS Edge) to avoid Playwright
 * browser downloads. Target: the Nuxt dev server on localhost:3000.
 */
export default defineConfig({
  testDir: './repros',
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    ...devices['Desktop Edge'],
    channel: 'msedge', // use installed Edge, not a Playwright-bundled browser
    baseURL: 'http://localhost:3000',
    trace: 'retain-on-failure',
  },
});
