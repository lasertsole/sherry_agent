import assert from 'node:assert/strict';
import { closeMenu } from '../lib/app.mjs';

export default {
  id: 'F14',
  name: 'Composer upload entry points',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('.insert-trigger');
    const body = await ui.waitText('Upload image', 15_000);
    assert.ok(body.includes('Upload audio'));
    assert.ok(body.includes('Upload video'));
    assert.ok(body.includes('Add file'));
    await closeMenu(ui);
  }
};
