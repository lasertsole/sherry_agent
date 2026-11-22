import { newSession, setAccessMode, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F05',
  name: 'HITL approval resumes the turn',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await setAccessMode(ui, 'Confirm changes');
    try {
      await sendMessage(ui, '[e2e:hitl] ask before running');
      await ui.waitText('terminal', 60_000);
      await ui.waitText('Approve', 30_000);
      await ui.clickText('Approve');
      await ui.waitText('Approved tool finished.', 60_000);
    } finally {
      // The HITL card owns the composer area; reset only when the picker is
      // reachable, and never mask the real failure with a cleanup error.
      try {
        await setAccessMode(ui, 'Auto edit');
      } catch {
        // Best effort.
      }
    }
  }
};
