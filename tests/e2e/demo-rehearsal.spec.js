// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Tessera demo rehearsal
 * ======================
 * Walks the exact path you'll show the VCs and fails loudly if anything that
 * would embarrass you on stage is broken: a page that won't render, a JS error,
 * a backend call that 4xx/5xx's, or a service round-trip that silently falls back
 * to "Backend not running".
 *
 * Requires the stack already running (docker-compose up) — see scripts/preflight.py.
 * Run:  cd tests && npm install && npx playwright install chromium && npm run rehearse
 * Debug a failure:  npm run report   (every step has a screenshot + trace)
 */

// Every page reachable from the top nav (data-page values in index.html).
const PAGES = [
  'home', 'solo', 'platform', 'predictive', 'earlywarning', 'cases',
  'tutorial', 'marketplace', 'change', 'problems', 'kb', 'skills',
  'people', 'payroll', 'onboarding', 'learning', 'expenses', 'demand', 'connectors',
];

/** Attach console / page-error / failed-backend-response collectors to a page. */
function watch(page) {
  const consoleErrors = [];
  const pageErrors = [];
  const failedBackend = [];
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text());
  });
  page.on('pageerror', (e) => pageErrors.push(e.message));
  page.on('response', (r) => {
    const url = r.url();
    const isBackend =
      url.includes('/api/') || url.includes(':8008') || /\/health(\b|$)/.test(url);
    if (isBackend && r.status() >= 400) {
      failedBackend.push(`${r.status()} ${r.request().method()} ${url}`);
    }
  });
  return { consoleErrors, pageErrors, failedBackend };
}

/** Navigate to a page the way a presenter would — click the visible nav link. */
async function gotoPage(page, id) {
  const link = page.locator(`.snav-a[data-page="${id}"] >> visible=true`).first();
  if (await link.count()) {
    await link.click();
  } else {
    // Fallback: the SPA exposes showPage() globally.
    await page.evaluate((p) => window.showPage(p), id);
  }
  await expect(page.locator(`#page-${id}`)).toHaveClass(/active/, { timeout: 8000 });
}

test.describe('Tessera demo rehearsal', () => {
  test('home page loads cleanly (no JS errors)', async ({ page }) => {
    const w = watch(page);
    await page.goto('/');
    await expect(page.locator('#page-home')).toHaveClass(/active/);
    // Give first-paint scripts a beat to throw if they're going to.
    await page.waitForTimeout(500);
    expect(w.pageErrors, `Uncaught JS errors on load:\n${w.pageErrors.join('\n')}`).toEqual([]);
  });

  test('every nav page renders without errors or failed backend calls', async ({ page }) => {
    const w = watch(page);
    await page.goto('/');

    for (const id of PAGES) {
      await test.step(`open "${id}"`, async () => {
        await gotoPage(page, id);
        await expect(page.locator(`#page-${id}`)).toBeVisible();
        await page.waitForTimeout(250); // let lazy per-page fetches fire
        await page.screenshot({ path: `screenshots/page-${id}.png`, fullPage: false });
      });
    }

    expect(
      w.failedBackend,
      `Backend calls that failed during the walk:\n${w.failedBackend.join('\n')}`,
    ).toEqual([]);
    expect(
      w.pageErrors,
      `Uncaught JS errors during the walk:\n${w.pageErrors.join('\n')}`,
    ).toEqual([]);
  });

  test('cost-benefit: "Build my agent stack" produces a recommendation', async ({ page }) => {
    await page.goto('/');
    await gotoPage(page, 'solo');
    // The form validates: #f-what must be filled or runRecommendation() bails
    // early and flashes the field red. A presenter types here first.
    await page.fill('#f-what', 'automate customer support ticket triage');
    const btn = page.locator('#analyze-btn');
    await expect(btn).toBeVisible();
    await btn.click();
    // runRecommendation plays a ~3s "thinking" animation, then renders #rec-output.
    await expect(page.locator('#rec-output')).toBeVisible({ timeout: 12000 });
    await expect(page.locator('#rec-output')).not.toBeEmpty();
  });

  test('payroll: "Run org payroll" round-trips through the gateway', async ({ page }) => {
    await page.goto('/');
    await gotoPage(page, 'payroll');

    const btn = page.locator('button:has-text("Run org payroll")').first();
    await expect(btn).toBeVisible();

    const [resp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/v1/payroll/run') && r.request().method() === 'POST',
        { timeout: 15000 },
      ),
      btn.click(),
    ]);
    expect(resp.status(), 'gateway should proxy payroll/run successfully').toBe(200);

    const result = page.locator('#payroll-run-result');
    await expect(result).toBeVisible();
    // The UI shows this exact string when the backend is unreachable — must NOT appear.
    await expect(result).not.toContainText('Backend not running');
  });

  test('governance: AI Governance dashboard fetches live data (:8008)', async ({ page }) => {
    await page.goto('/');
    // Opening "earlywarning" triggers govStartPolling() -> GET :8008/dashboard/demo-org
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().includes('/dashboard/'), { timeout: 15000 }),
      gotoPage(page, 'earlywarning'),
    ]);
    expect(resp.status(), 'governance dashboard fetch should succeed').toBeLessThan(400);
  });
});
