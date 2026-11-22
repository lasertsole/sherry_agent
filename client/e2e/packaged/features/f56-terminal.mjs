import { Key } from '../lib/driver.mjs';
import { openToolbox, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F56',
  name: 'Operator terminal runs echo',
  status: 'implemented',
  async run({ ui }) {
    await openToolbox(ui, 'terminal');
    await ui.find('[data-test="terminal-panel"]', 15_000);
    const input = await ui.find('[data-test="terminal-input"]', 15_000).catch(() => null);
    if (input) {
      await input.sendKeys('echo E2E_TERMINAL_OK');
      await input.sendKeys(Key.ENTER);
    } else {
      const box = await ui.find('[data-test="terminal-panel"] [contenteditable="true"]', 15_000);
      await box.sendKeys('echo E2E_TERMINAL_OK');
      await box.sendKeys(Key.ENTER);
    }
    await ui.waitText('E2E_TERMINAL_OK', 30_000);
    await closeRightTab(ui);
  }
};
