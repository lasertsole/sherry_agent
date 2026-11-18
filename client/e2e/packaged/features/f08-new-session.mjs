import assert from 'node:assert/strict';
import { newSession, sessionRowSelector } from '../lib/app.mjs';

export default {
  id: 'F08',
  name: 'Create a new session',
  status: 'implemented',
  async run({ ui }) {
    const sid = await newSession(ui);
    assert.ok(sid && sid !== 'home');
    await ui.find(sessionRowSelector(sid), 30_000);
    assert.match(await ui.pathname(), new RegExp(`/home/${sid}$`));
  }
};
