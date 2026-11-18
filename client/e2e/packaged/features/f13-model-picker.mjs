import assert from 'node:assert/strict';
import { closeMenu } from '../lib/app.mjs';

export default {
  id: 'F13',
  name: 'Session model picker',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('.model-trigger');
    const body = await ui.waitText('Main model', 15_000);
    assert.ok(body.includes('Follow the model config'));
    await closeMenu(ui);
  }
};
