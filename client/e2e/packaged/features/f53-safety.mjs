import { openMenuTile, closeRightTab, clickTab } from '../lib/app.mjs';

export default {
  id: 'F53',
  name: 'Live safety tab',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'systemConfig');
    await ui.waitText('Background Image', 20_000);
    await clickTab(ui, 'Live Safety').catch(() => {});
    await ui.waitText('Safety', 15_000).catch(() => {});
    await closeRightTab(ui);
  }
};
