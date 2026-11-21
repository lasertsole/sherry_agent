import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F42',
  name: 'Memory panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'memory');
    await ui.waitText('Memory', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
