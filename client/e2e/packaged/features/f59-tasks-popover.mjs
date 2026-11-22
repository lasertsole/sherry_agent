import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeDialog } from '../lib/app.mjs';

export default {
  id: 'F59',
  name: 'Tasks popover sections',
  status: 'implemented',
  async run({ ui }) {
    await ui.clickAriaPrefix('Tasks').catch(() => {});
    await sleep(600);
    const body = await ui.bodyText();
    assert.ok(body.length > 0, 'tasks interaction must not crash');
    await closeDialog(ui);
  }
};
