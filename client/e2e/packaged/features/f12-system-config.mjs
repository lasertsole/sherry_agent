import { ensureSession, openMenu } from '../lib/app.mjs';

export default {
  id: 'F12',
  name: 'Open System Config',
  status: 'implemented',
  async run({ ui }) {
    await ensureSession(ui);
    await openMenu(ui);
    await ui.click('[data-test="settings-menu-systemConfig"]');
    // The System Config panel opens on its Background tab; the skeleton is the
    // panel's own root hook (the internal tab strip is horizontally scrollable).
    await ui.waitText('Background Image', 20_000);
  }
};
