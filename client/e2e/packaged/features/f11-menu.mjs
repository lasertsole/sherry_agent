import assert from 'node:assert/strict';
import { closeMenu, ensureSession, openMenu } from '../lib/app.mjs';

export default {
  id: 'F11',
  name: 'Settings menu grid',
  status: 'implemented',
  async run({ ui }) {
    await ensureSession(ui);
    await openMenu(ui);
    const body = await ui.waitText('System Config', 15_000);
    assert.ok(body.includes('Skills'));
    assert.ok(body.includes('Memory'));
    await closeMenu(ui);
  }
};
