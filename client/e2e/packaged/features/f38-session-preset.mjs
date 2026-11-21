import { sleep } from '../lib/driver.mjs';
import { closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F38',
  name: 'Session preset view',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('[data-test="session-preset-trigger"]');
    await sleep(800);
    await closeRightTab(ui);
  }
};
