import { test, Page, ConsoleMessage } from '@playwright/test';

/**
 * Repro round 2: seed a real conversation first (send once, wait for stream to
 * finish so bubbles render), then send a second message and watch whether ALL
 * previously-rendered bubbles disappear ~1s after the 2nd send.
 *
 * NOTE (Aug-2026, current live app):
 *  - Live Nuxt dev server runs on port 5177 (was 3000 pre-refactor).
 *  - ChatBox hides TOOL role AND AI messages with empty content, so after a send
 *    the bubble delta is +1 (user only) until the AI actually emits text — the
 *    historical "+2 bubbles per send" assumption no longer holds. This spec now
 *    waits on a stable non-zero count + .send-button reappearing, not a fixed +2.
 */
const APP_URL = 'http://localhost:5177';

interface SnapshotLog {
  t: number;
  kind: 'pageerror' | 'console' | 'dom-count';
  msg: string;
}

async function monitor(page: Page, log: SnapshotLog[]) {
  const t0 = Date.now();
  page.on('pageerror', (err) => {
    log.push({ t: Date.now() - t0, kind: 'pageerror', msg: String(err?.stack || err) });
  });
  page.on('console', (c: ConsoleMessage) => {
    if (['error', 'warning'].includes(c.type())) {
      log.push({ t: Date.now() - t0, kind: 'console', msg: `[${c.type()}] ${c.text()}` });
    }
  });
}

async function countMessages(page: Page): Promise<number> {
  return page.locator('div.w-fit.p-3.text-sm.break-words, div.w-fit.p-3.text-sm').count();
}

async function sendText(page: Page, text: string) {
  const inputBox = page.locator('div.inputBox[contenteditable="true"]');
  await inputBox.click();
  await page.keyboard.type(text);
  await page.locator('button.send').click();
}

test('seed a conversation then send again; watch for list clear', async ({ page }) => {
  const log: SnapshotLog[] = [];
  await monitor(page, log);
  const t0 = Date.now();

  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  const inputBox = page.locator('div[contenteditable="true"]');
  await inputBox.waitFor({ state: 'attached', timeout: 30000 });

  // Wait for init to settle.
  await page.waitForTimeout(2000);
  const baseline = await countMessages(page);
  console.log('BASELINE =', baseline);

  // --- Send #1, let streaming finish ---
  await sendText(page, '你好');
  console.log('send#1 done');

  // Wait until at least one new bubble renders (user bubble). NOTE: the AI
  // placeholder with empty content is HIDDEN by ChatBox until it emits text, so
  // after a send we may only see +1 (the user bubble) until text arrives.
  let after1 = await countMessages(page);
  for (let i = 0; i < 40 && after1 < baseline + 1; i++) {
    await page.waitForTimeout(250);
    after1 = await countMessages(page);
  }
  console.log('after send#1 count =', after1);

  // Optionally wait for the AI to actually stream a full reply (sending flag off
  // => the blue 发送 button returns; while generating it is a red 停止 button).
  try {
    await page.locator('button.send').waitFor({ state: 'visible', timeout: 20000 });
    // The 发送 button has a non-empty label ("发送"); 停止 also matches .send.
    // Re-confirm generation really ended by grepping the visible label.
    const btnLabel = await page.locator('button.send').innerText().catch(() => '');
    console.log('send#1 settled; button label =', JSON.stringify(btnLabel));
    if (btnLabel.trim() === '停止') {
      // Still generating — give it up to 15 more seconds.
      await page.locator('button.send:has-text("发送")').waitFor({ state: 'visible', timeout: 15000 });
    }
  } catch {
    console.log('send#1 still streaming/stopped after 20s');
  }
  await page.waitForTimeout(1000);
  const stableBefore2 = await countMessages(page);
  console.log('count before send#2 =', stableBefore2);

  // --- Send #2 (the reported trigger) ---
  await sendText(page, '晚上好');
  console.log('send#2 done');

  // Poll DOM count frequently to catch the ~1s clearing moment.
  const window = 8000;
  const start = Date.now();
  let minCount = stableBefore2;
  let minAt = -1;
  let last = stableBefore2;
  let sawEq0 = false;
  while (Date.now() - start < window) {
    await page.waitForTimeout(100);
    const n = await countMessages(page);
    if (n !== last) {
      log.push({ t: Date.now() - t0, kind: 'dom-count', msg: `count ${last} -> ${n}` });
      last = n;
    }
    if (n === 0) {
      sawEq0 = true;
      minAt = Date.now() - t0;
    }
    if (n < minCount) minCount = n;
    if (sawEq0) break;
  }

  await page.waitForTimeout(2000);
  const finalCount = await countMessages(page);
  console.log('MIN after send#2 =', minCount, 'minAt=', minAt, 'sawEq0=', sawEq0);
  console.log('FINAL =', finalCount);

  console.log('================ EVIDENCE ================');
  for (const e of log) console.log(`[t+${e.t}ms] ${e.kind}: ${e.msg}`);
  console.log('==========================================');

  if (sawEq0) {
    console.log('REPRODUCED: list hit 0 after send#2');
  } else {
    console.log('NOT reproduced: list never hit 0');
  }
  await page.screenshot({ path: 'repros/evidence-round2.png', fullPage: true });
});
