import assert from 'node:assert/strict';
import { currentSid, newSession, switchToSession } from '../lib/app.mjs';

export default {
  id: 'F09',
  name: 'Switch between sessions',
  status: 'implemented',
  async run({ ui }) {
    const previous = await currentSid(ui);
    const second = await newSession(ui);
    await switchToSession(ui, previous);
    assert.ok((await ui.pathname()).endsWith(`/home/${previous}`));
    await switchToSession(ui, second);
    assert.ok((await ui.pathname()).endsWith(`/home/${second}`));
  }
};
