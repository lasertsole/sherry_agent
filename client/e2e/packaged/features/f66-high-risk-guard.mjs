import { closeRightTab, ensureSession } from '../lib/app.mjs';

export default {
  id: 'F66',
  name: 'High-risk guard panel',
  status: 'implemented',
  async run({ ui }) {
    await ensureSession(ui);
    await ui.click('[data-test="high-risk-guard-button"]');
    await ui.find('[data-test="high-risk-keyword-input"]', 15_000);
    await closeRightTab(ui);
  }
};
