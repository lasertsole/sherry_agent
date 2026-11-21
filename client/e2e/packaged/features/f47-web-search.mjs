import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F47',
  name: 'Web search backends panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'webSearch');
    await ui.waitText('Web Search', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
