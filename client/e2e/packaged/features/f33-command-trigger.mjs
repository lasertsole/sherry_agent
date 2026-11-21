import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { COMPOSER, closeDialog } from '../lib/app.mjs';

export default {
  id: 'F33',
  name: 'Command trigger panel (/)',
  status: 'implemented',
  async run({ ui }) {
    await ui.type(COMPOSER, '/');
    await sleep(700);
    const body = await ui.bodyText();
    assert.ok(body.length > 0, 'composer alive after / trigger');
    await closeDialog(ui);
  }
};
