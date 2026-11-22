import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F06',
  name: 'Provider error surfaces in chat',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:error] fail this call');
    const text = await ui.waitText('Reply failed', 120_000);
    // Distinguish the mock provider failure from a transport/socket error.
    if (!text.includes('mock model failure') && !text.includes('500')) {
      throw new Error('error surface did not name the mock provider failure');
    }
  }
};
