import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F37',
  name: 'Notifications tab',
  status: 'implemented',
  async run({ ui }) {
    await ui.clickAriaPrefix('Notifications');
    await sleep(800);
    assert.ok(await ui.exists('[data-test="notification-panel"]'), 'notification panel should render');
    await closeRightTab(ui);
  }
};
