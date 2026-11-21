import { newSession, sendMessage, pressChord } from '../lib/app.mjs';

export default {
  id: 'F23',
  name: 'In-conversation find bar',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] needle-hello');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    await pressChord(ui, 'F', { ctrl: true, shift: true, code: 'KeyF' });
    await ui.find('[data-test="conversation-find-bar"]', 10_000);
    await ui.type('[data-test="conversation-find-input"]', 'needle-hello');
    await ui.waitText('1/', 12_000);
    await ui.click('[data-test="conversation-find-close"]');
    await ui.waitGone('[data-test="conversation-find-bar"]', 8_000);
  }
};
