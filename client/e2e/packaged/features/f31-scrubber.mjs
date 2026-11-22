import assert from 'node:assert/strict';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F31',
  name: 'Transcript persists across turns',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] scrub one');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    await sendMessage(ui, '[e2e:plain] scrub two');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    const body = await ui.bodyText();
    assert.ok(body.includes('scrub one'), 'earlier turn stays in the transcript');
  }
};
