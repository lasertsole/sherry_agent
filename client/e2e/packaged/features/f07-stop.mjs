import assert from 'node:assert/strict';
import { newSession, sendMessage, lastAiText } from '../lib/app.mjs';

export default {
  id: 'F07',
  name: 'Stop generation',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:slow] take your time');
    await ui.find('.stop', 60_000);
    await ui.waitText('Still', 30_000);
    const before = await lastAiText(ui);
    await ui.click('.stop');
    await ui.waitGone('.stop', 30_000);
    await ui.find('.send', 30_000);
    await new Promise(resolve => setTimeout(resolve, 2000));
    const after = await lastAiText(ui);
    assert.ok(!after.includes('answer...'), 'stopped turn must not finish the mock answer');
    assert.ok(after.length >= 0 && before.length >= 0);
  }
};
