import { newSession, sendWithEnter } from '../lib/app.mjs';

export default {
  id: 'F02',
  name: 'Send message via Enter',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendWithEnter(ui, '[e2e:plain] enter send');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
  }
};
