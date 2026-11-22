import { openToolbox, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F58',
  name: 'Browser panel renders',
  status: 'implemented',
  async run({ ui }) {
    await openToolbox(ui, 'browser');
    await ui.find('[data-test="browser-panel"]', 15_000);
    await closeRightTab(ui);
  }
};
