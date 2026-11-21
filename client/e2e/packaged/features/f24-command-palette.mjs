import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { closeDialog, pressChord } from '../lib/app.mjs';

export default {
  id: 'F24',
  name: 'Command palette (Ctrl+K)',
  status: 'implemented',
  async run({ ui }) {
    await pressChord(ui, 'k', { ctrl: true });
    await ui.find('[data-test="command-palette"]', 10_000);
    await ui.type('[data-test="command-palette-input"]', 'session');
    await sleep(600);
    const hasResults = await ui.exists('[data-test="command-palette-results"]');
    const hasRecent = await ui.exists('[data-test="command-palette-recent"]');
    assert.ok(hasResults || hasRecent, 'palette should show results or the recent state');
    await closeDialog(ui);
    await ui.waitGone('[data-test="command-palette"]', 8_000);
  }
};
