import { ensureSession } from '../lib/app.mjs';

export default {
  id: 'F50',
  name: 'Meeting list and meeting page',
  status: 'implemented',
  async run({ ui }) {
    await ui.click('[data-test="nav-meetings"]');
    await ui.find('[data-test="meeting-list-panel"]', 15_000);
    await ui.find('[data-test="meeting-card"]', 20_000);
    await ui.click('[data-test="meeting-card"]');
    await ui.find('[data-test="meeting-page"]', 20_000);
    await ui.find('[data-test="meeting-stream"]', 10_000);
    await ui.click('[data-test="meeting-back"]');
    await ui.find('[data-test="session-sidebar"]', 15_000);
    // Meeting navigation leaves the shell without a composer; restore a chat
    // session so the remaining stubs start from the normal chat state.
    await ensureSession(ui);
  }
};
