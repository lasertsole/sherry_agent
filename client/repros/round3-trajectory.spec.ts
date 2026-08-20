import { test, Page, ConsoleMessage, Request } from '@playwright/test';

/**
 * Round 3: decisive trajectory capture with CORRECT selectors.
 *  - input:  div.inputBox[contenteditable="true"]
 *  - send:   button.send (PrimeVue Button, disabled when empty/sending)
 *  - bubbles: div.w-fit.p-3.text-sm.break-words (ChatBox message bubble)
 *
 * Goals:
 *  1. Confirm sequential sends each register (the user bubble always appears;
 *     the AI bubble only after it emits text — ChatBox hides empty-AI placeholders).
 *  2. Fine-grained polling captures any transient drop-to-zero (~1s after send).
 *  3. Log all history/ws network + pageerrors + console errors.
 *
 * NOTE (Aug-2026): live Nuxt dev server is on port 5177 (was 3000 pre-refactor).
 */
const APP = 'http://localhost:5177';
const INPUT = 'div.inputBox[contenteditable="true"]';
const SEND = 'button.send';
const BUBBLE = 'div.w-fit.p-3.text-sm.break-words';

async function bubbleCount(page: Page): Promise<number> {
  return page.locator(BUBBLE).count();
}

async function logBubbles(page: Page, label: string): Promise<number> {
  const items = await page.locator(BUBBLE).all();
  const n = items.length;
  const texts: string[] = [];
  for (let i = 0; i < Math.min(items.length, 6); i++) {
    texts.push((await items[i].innerText().catch(() => '')).slice(0, 40));
  }
  console.log(`${label}: bubbles=${n} ${texts.length ? '| ' + texts.join(' || ') : ''}`);
  return n;
}

test('sequential sends with correct selectors; capture clear events', async ({ page }) => {
  const t0 = Date.now();
  const mark = (s: string) => console.log(`[t+${Date.now() - t0}ms] ${s}`);

  page.on('request', (r: Request) => {
    const u = r.url();
    if (u.includes('/get_history_by_turn_page') || u.includes('/sessions/') || u.includes('/ws')) {
      mark(`REQ ${r.method()} ${u}`);
    }
  });
  page.on('response', async (r) => {
    const u = r.url();
    if (u.includes('/get_history_by_turn_page')) {
      let body = '';
      try { const j = await r.json(); body = `rows=${Array.isArray(j?.data) ? j.data.length : '?'}`; } catch { body = '<?>'; }
      mark(`RES ${r.status()} ${u} (${body})`);
    }
  });
  page.on('pageerror', (e) => mark(`PAGEERROR ${String(e?.stack || e)}`));
  page.on('console', (c: ConsoleMessage) => {
    if (['error', 'warning'].includes(c.type())) mark(`CONSOLE[${c.type()}] ${c.text()}`);
  });

  await page.goto(APP, { waitUntil: 'domcontentloaded' });
  await page.locator(INPUT).waitFor({ state: 'attached', timeout: 30000 });
  await page.waitForTimeout(3000);
  await logBubbles(page, 'baseline');

  async function send(text: string): Promise<number> {
    const before = await bubbleCount(page);
    const input = page.locator(INPUT);
    await input.click();
    await page.keyboard.type(text, { delay: 30 });
    const disabledBefore = await page.locator(SEND).isDisabled().catch(() => true);
    await page.locator(SEND).click();
    const disabledAfter = await page.locator(SEND).isDisabled().catch(() => true);
    mark(`sent "${text}" before=${before} sendDisabledBefore=${disabledBefore} ->after=${disabledAfter}`);

    const deadline = Date.now() + 20000;
    let n = before;
    let lastDelta = 0;
    let minObserved = before;
    let aiBubbleSeen = false;
    while (Date.now() < deadline) {
      await page.waitForTimeout(60);
      n = await bubbleCount(page);
      if (n < minObserved) {
        minObserved = n;
        mark(`!! DIP below prior-min: ${minObserved} @ t+${Date.now() - t0}`);
      }
      lastDelta = n - before;
      // The user bubble appears immediately (+1). The AI bubble appears once the
      // model emits text (empty-AI placeholders are HIDDEN by ChatBox), so wait
      // for that second bubble OR the generation to settle (send button comes back).
      if (lastDelta >= 2) {
        aiBubbleSeen = true;
        break;
      }
      const settled = await page
        .locator(`${SEND}:has-text("发送")`)
        .waitFor({ state: 'visible', timeout: 500 })
        .then(() => true)
        .catch(() => false);
      if (settled && lastDelta >= 1) break;
    }
    mark(`after "${text}": +${lastDelta} -> total ${n} (minSeen=${minObserved}, aiBubble=${aiBubbleSeen})`);
    await logBubbles(page, `post-send "${text}"`);
    return n;
  }

  await send('第一条');
  await page.waitForTimeout(2000);
  await logBubbles(page, 'settled after 第一条');

  await send('晚上好');
  await page.waitForTimeout(2000);
  await logBubbles(page, 'settled after 晚上好#1');

  await send('晚上好');
  await page.waitForTimeout(2000);
  await logBubbles(page, 'settled after 晚上好#2');

  await page.screenshot({ path: 'repros/evidence-round3.png', fullPage: true });
  console.log('DONE');
});
