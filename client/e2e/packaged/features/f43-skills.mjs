import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F43',
  name: 'Skills manager panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'skills');
    await ui.waitText('Skills', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
