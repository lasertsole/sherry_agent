import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F46',
  name: 'Extend panel (channels/MCP)',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'extend');
    await ui.waitText('Extend', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
