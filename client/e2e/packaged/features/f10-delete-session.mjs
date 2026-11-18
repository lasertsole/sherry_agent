import assert from 'node:assert/strict';
import { deleteSession, newSession } from '../lib/app.mjs';

export default {
  id: 'F10',
  name: 'Delete a session',
  status: 'implemented',
  async run({ ui }) {
    const disposable = await newSession(ui);
    await deleteSession(ui, disposable);
    const body = await ui.bodyText();
    assert.ok(!body.includes(disposable), 'deleted session row should disappear');
    assert.ok(!(await ui.pathname()).endsWith(`/home/${disposable}`));
  }
};
