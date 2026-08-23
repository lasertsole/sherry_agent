import { test, Page, ConsoleMessage } from '@playwright/test';

const APP_URL = 'http://localhost:3000';

async function monitor(page: Page, log: string[]) {
  page.on('pageerror', (err) => log.push(`pageerror: ${String(err?.stack || err)}`));
  page.on('console', (c: ConsoleMessage) => {
    if (['error', 'warning', 'assert'].includes(c.type())) log.push(`console[${c.type()}]: ${c.text()}`);
  });
}

test('dump page state on load', async ({ page }) => {
  const log: string[] = [];
  await monitor(page, log);
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  console.log('URL =', page.url());
  const body = await page.locator('body').innerText();
  console.log('BODY TEXT (first 2000 chars) =====');
  console.log(body.slice(0, 2000));
  console.log('END BODY =====');
  // list all contenteditable and top-level visible elements
  const ce = await page.locator('[contenteditable]').count();
  console.log('contenteditable count =', ce);
  const inputs = await page.locator('input, textarea').count();
  console.log('input/textarea count =', inputs);
  // look for login / auth clues
  const login = await page.locator('button, a').allInnerTexts();
  console.log('BUTTONS/A LINKS =', JSON.stringify(login.slice(0, 30)));
  await page.screenshot({ path: 'repros/diag-state.png', fullPage: true });
  console.log('CONSOLE/PAGEERR =', JSON.stringify(log));
});
