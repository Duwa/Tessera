// @ts-check
const { defineConfig, devices } = require('@playwright/test');

/**
 * Demo-rehearsal config for the Tessera HCM platform.
 *
 * The app is expected to be ALREADY RUNNING (docker-compose up) — this suite
 * does NOT start it. nginx serves the frontend on http://localhost (port 80)
 * and proxies /api/ to the gateway, so that's the base URL.
 *
 * Override for a remote/other host:  BASE_URL=http://1.2.3.4 npx playwright test
 */
module.exports = defineConfig({
  testDir: './e2e',
  fullyParallel: false,           // a demo walk-through is a sequence, keep it ordered
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  timeout: 30000,
  expect: { timeout: 8000 },
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost',
    trace: 'on',                  // every run records a trace — replay failures step by step
    screenshot: 'on',
    video: 'off',
    actionTimeout: 8000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
