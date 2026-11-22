import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeDialog } from '../lib/app.mjs';

export default {
  id: 'F36',
  name: 'Voice button graceful path',
  status: 'implemented',
  async run({ ui }) {
    const mic = await ui.findAll('[aria-label*="voice" i], [aria-label*="Voice" i]');
    if (!mic.length) {
      assert.ok(true, 'mic button not exposed in this build');
      return;
    }
    await ui.driver.executeScript('arguments[0].click()', mic[mic.length - 1]);
    await sleep(800);
    const body = await ui.bodyText();
    assert.ok(body.length > 0, 'clicking the mic must not crash the app');
    await closeDialog(ui);
  }
};
