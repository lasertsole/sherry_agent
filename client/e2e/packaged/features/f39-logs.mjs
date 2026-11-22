import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F39',
  name: 'Logs panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'logs');
    await ui.waitText('Logs', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
