import assert from 'node:assert/strict';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F27',
  name: 'Token usage meta under a bubble',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] count me');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    const body = await ui.bodyText();
    assert.match(body, /\d+ tokens/, 'turn should report token usage');
  }
};
