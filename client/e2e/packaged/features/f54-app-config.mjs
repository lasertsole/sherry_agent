import { openMenuTile, closeRightTab, clickTab } from '../lib/app.mjs';

export default {
  id: 'F54',
  name: 'App Config tab entries',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'systemConfig');
    await ui.waitText('Background Image', 20_000);
    await clickTab(ui, 'App Config');
    await ui.find('[data-test^="sherry-entry-"]', 20_000);
    await closeRightTab(ui);
  }
};
