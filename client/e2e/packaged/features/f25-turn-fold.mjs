import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F25',
  name: 'Settled turn history fold',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:tools] fold me');
    await ui.waitText('Tool result received from the real backend.', 60_000);
    await ui.find('[data-test="turn-history-status"]', 20_000);
    await ui.click('[data-test="turn-history-status"]');
    await ui.waitText('read_file', 15_000);
  }
};
