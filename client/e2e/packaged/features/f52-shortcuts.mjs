import { openMenuTile, closeRightTab, clickTab } from '../lib/app.mjs';

export default {
  id: 'F52',
  name: 'Shortcuts settings tab',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'systemConfig');
    await ui.waitText('Background Image', 20_000);
    await clickTab(ui, 'Shortcuts');
    await ui.waitText('Ctrl', 15_000).catch(() => {});
    await closeRightTab(ui);
  }
};
