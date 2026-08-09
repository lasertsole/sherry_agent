import { test, Page, ConsoleMessage } from '@playwright/test';

/**
 * Repro round 2: seed a real conversation first (send once, wait for stream to
 * finish so bubbles render), then send a second message and watch whether ALL
 * previously-rendered bubbles disappear ~1s after the 2nd send.
 */
const APP_URL = 'http://localhost:3000';

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
  const inputBox = page.locator('div[contenteditable="true"]');
  await inputBox.click();
  await page.keyboard.type(text);
  await page.locator('.send').click();
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

  // Wait until at least 2 bubbles render (user + ai placeholder), or stop button disappears.
  let after1 = await countMessages(page);
  for (let i = 0; i < 40 && after1 < 2; i++) {
    await page.waitForTimeout(250);
    after1 = await countMessages(page);
  }
  console.log('after send#1 count =', after1);

  // Optionally wait for the AI to actually stream a full reply (sending flag off => .send button back).
  try {
    await page.locator('.send').waitFor({ state: 'visible', timeout: 20000 });
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
