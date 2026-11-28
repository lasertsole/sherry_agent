import { ensureSession } from '../lib/app.mjs';

export default {
  id: 'F67',
  name: 'Meeting create dialog cancel path',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('[data-test="nav-meetings"]');
    await ui.find('[data-test="meeting-list-panel"]', 15_000);
    await ui.click('[data-test="meeting-create-open"]');
    await ui.find('[data-test="meeting-create-dialog"]', 15_000);
    await ui.find('[data-test="meeting-topic"]', 10_000);
    await ui.click('[data-test="meeting-create-cancel"]');
    await ui.waitGone('[data-test="meeting-create-dialog"]', 10_000);
    await ensureSession(ui);
  }
};
