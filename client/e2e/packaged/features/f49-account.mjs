import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F49',
  name: 'Account panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'account');
    // Older packaged builds render the same visible auth state without the
    // auth-status data-test hook, so assert the user-visible text instead.
    await ui.waitText('Login protection is', 20_000);
    await closeRightTab(ui);
  }
};
