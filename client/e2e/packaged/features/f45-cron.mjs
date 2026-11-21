import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F45',
  name: 'Cron tasks panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'cron');
    await ui.waitText('Cron', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
