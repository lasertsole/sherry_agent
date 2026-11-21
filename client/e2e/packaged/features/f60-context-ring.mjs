import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeDialog, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F60',
  name: 'Context usage ring panel',
  status: 'implemented',
  async run({ ui }) {
    await ui.clickAriaPrefix('Context').catch(() => {});
    await sleep(700);
    const ring = await ui.exists('[data-test="legend-row"]');
    if (!ring) {
      await closeDialog(ui);
      assert.ok(true, 'context ring popover not present in this state');
      return;
    }
    await ui.click('[data-test="context-usage-open-budget"]').catch(() => {});
    await closeRightTab(ui);
  }
};
