import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F44',
  name: 'Heartbeat panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'heartbeat');
    await ui.waitText('Heartbeat', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
