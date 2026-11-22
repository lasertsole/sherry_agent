import assert from 'node:assert/strict';
import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F49',
  name: 'Account panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'account');
    await ui.waitText('Account', 20_000).catch(() => {});
    assert.ok(await ui.exists('[data-test="auth-status"]'), 'auth status should render');
    await closeRightTab(ui);
  }
};
