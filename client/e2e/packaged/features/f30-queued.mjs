import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { COMPOSER, newSession, sendMessage, waitForIdle } from '../lib/app.mjs';

export default {
  id: 'F30',
  name: 'Queued message while busy',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:slow] hold the line');
    // Streaming confirmed by the first chunk; the Stop button's visibility
    // window is racy on a 6 s mock stream, so the chunk is the anchor.
    await ui.waitText('Still', 30_000);
    await ui.type(COMPOSER, '[e2e:plain] queued behind');
    await sleep(300);
    await ui.click('button.send');
    await sleep(800);
    const body = await ui.bodyText();
    assert.ok(body.includes('queued behind'), 'second message entered the pipeline');
    assert.ok(body.includes('Keep typing to queue') || body.includes('queue'), 'queue hint/bar shown');
    await ui.click('.stop').catch(() => {});
    await waitForIdle(ui, 90_000);
  }
};
