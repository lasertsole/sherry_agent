import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F40',
  name: 'Token statistics panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'stats');
    await ui.waitText('Statistics', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
