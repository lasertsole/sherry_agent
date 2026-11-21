import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeDialog } from '../lib/app.mjs';

export default {
  id: 'F19',
  name: 'Session row context menu',
  status: 'implemented',
  async run({ ui }) {
    const row = await ui.find('[data-session-id]', 15_000);
    await ui.driver.executeScript(
      "arguments[0].dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))",
      row
    );
    await sleep(500);
    assert.ok(await ui.exists('[data-test="session-context-menu"]'), 'context menu should open');
    await closeDialog(ui);
  }
};
