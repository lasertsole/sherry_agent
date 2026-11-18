import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F01',
  name: 'Send message via button',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] button send');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
  }
};
