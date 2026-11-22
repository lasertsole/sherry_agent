import assert from 'node:assert/strict';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F03',
  name: 'Streaming assistant reply',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:stream]');
    await ui.waitText('Streaming', 60_000);
    const partial = await ui.bodyText();
    assert.ok(
      !partial.includes('Streaming one chunk at a time.'),
      'expected to observe the partial answer before the final chunk'
    );
    await ui.waitText('Streaming one chunk at a time.', 60_000);
  }
};
