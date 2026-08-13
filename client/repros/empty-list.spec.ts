import { test, Page, ConsoleMessage } from '@playwright/test';

/**
 * Repro for: 发送「晚上好」后约 1 秒，对话消息列表全部清空。
 *
 * Goal: capture runtime evidence at the exact clearing moment —
 *   (1) any pageerror (render crash in ChatBox v-for / safeHtml)
 *   (2) any console.error/warn
 *   (3) message-list DOM child count over time
 *   (4) attempt to read the actual Vue reactive `messages` array length
 *
 * Use installed Edge (channel) to avoid browser download.
 */

const APP_URL = 'http://localhost:3000';

// Snapshot log buffer
interface SnapshotLog {
  t: number; // ms since start
  kind: 'pageerror' | 'console' | 'dom-count' | 'vue-messages';
  msg: string;
}

async function monitor(page: Page, log: SnapshotLog[]) {
  const t0 = Date.now();
  page.on('pageerror', (err) => {
    log.push({ t: Date.now() - t0, kind: 'pageerror', msg: String(err?.stack || err) });
  });
  page.on('console', (c: ConsoleMessage) => {
    if (['error', 'warning', 'assert'].includes(c.type())) {
      log.push({ t: Date.now() - t0, kind: 'console', msg: `[${c.type()}] ${c.text()}` });
    }
  });
}

/** Count rendered chat message bubbles (each message row has exactly one bubble). */
async function countMessages(page: Page): Promise<number> {
  return page.locator('div.w-fit.p-3.text-sm.break-words, div.w-fit.p-3.text-sm').count();
}

/**
 * Try to read the Vue app's currentSession messages length via the
 * Vue devtools global hook is complex; instead we inspect the DOM which
 * is what the user actually sees (the bug is "list became empty").
 */
test('send 晚上好 and capture list-clear evidence', async ({ page }) => {
  const log: SnapshotLog[] = [];
  await monitor(page, log);

  const t0 = Date.now();
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

  // Wait for the chat UI to mount: input box present.
  const inputBox = page.locator('div[contenteditable="true"]');
  await inputBox.waitFor({ state: 'attached', timeout: 30000 });

  // Wait a little for character/session init to settle.
  await page.waitForTimeout(2000);

  // Baseline: message count before sending.
  const beforeCount = await countMessages(page);
  log.push({ t: Date.now() - t0, kind: 'dom-count', msg: `baseline count=${beforeCount}` });
  console.log('BASELINE count =', beforeCount);

  // Type into the contenteditable box.
  await inputBox.click();
  await page.keyboard.type('晚上好');

  // Click the 发送 button (PrimeVue Button label, class .send).
  await page.locator('.send').click();
  console.log('SENT 晚上好');

  // Poll the DOM message count every 100ms for up to 12s to catch the clearing moment.
  const eventWindow = 5000;
  const start = Date.now();
  let minCount = beforeCount;
  let clearedAt = -1;
  let lastCount = beforeCount;
  while (Date.now() - start < eventWindow) {
    // eslint-disable-next-line no-await-in-loop
    await page.waitForTimeout(100);
    // eslint-disable-next-line no-await-in-loop
    const n = await countMessages(page);
    if (n !== lastCount) {
      log.push({ t: Date.now() - t0, kind: 'dom-count', msg: `count ${lastCount} -> ${n}` });
      lastCount = n;
    }
    if (n < minCount) {
      minCount = n;
      clearedAt = Date.now() - t0;
    }
    if (n === 0) break;
  }

  console.log('MIN count =', minCount, 'at t=', clearedAt, 'ms after load');
  console.log('LAST count =', lastCount);

  // Pause so streaming can finish and we can dump full evidence.
  await page.waitForTimeout(3000);
  const finalCount = await countMessages(page);
  log.push({ t: Date.now() - t0, kind: 'dom-count', msg: `final count=${finalCount}` });
  console.log('FINAL count =', finalCount);

  // Dump all evidence.
  console.log('================ EVIDENCE ================');
  for (const e of log) {
    console.log(`[t+${e.t}ms] ${e.kind}: ${e.msg}`);
  }
  console.log('==========================================');

  // The test itself: we specifically WANT to observe the bug, so we do not
  // hard-assert; we print PASS/FAIL based on observed min count.
  if (minCount === 0) {
    console.log('REPRODUCED: message list became empty (minCount=0)');
  } else {
    console.log(`NOT reproduced: minCount=${minCount} (expect >0 for a non-buggy run)`);
  }

  // Screenshot for the record.
  await page.screenshot({ path: 'repros/evidence-final.png', fullPage: true });
});
