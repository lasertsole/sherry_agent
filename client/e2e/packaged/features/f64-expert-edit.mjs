import { closeRightTab, ensureSessionList } from '../lib/app.mjs';

export default {
  id: 'F64',
  name: 'Expert list and editor panel',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('[data-test="nav-experts"]');
    await ui.find('[data-test="expert-list-panel"]', 15_000);
    await ui.find('[data-test="expert-card"]', 20_000);
    await ui.click('[data-test="expert-card"]');
    await ui.find('[data-test="expert-edit-panel"]', 15_000);
    await ui.find('[data-test="expert-form-name"]', 10_000);
    await closeRightTab(ui);
    await ensureSessionList(ui);
  }
};
