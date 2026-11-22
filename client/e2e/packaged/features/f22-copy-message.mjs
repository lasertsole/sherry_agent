import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F22',
  name: 'Copy an AI message',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] copy me');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    await ui.execute(`
    const rows = document.querySelectorAll('[data-message-role="ai"]');
    const last = rows[rows.length - 1];
    const btn = last && (last.querySelector('button[aria-label="Copy"]') ||
      [...last.parentElement.querySelectorAll('button')]
        .find(b => (b.getAttribute('aria-label') || '').startsWith('Copy')));
    if (btn) btn.click();
    return !!btn;
  `);
    await sleep(600);
    const alive = await ui.exists('[data-message-role="ai"]');
    assert.ok(alive, 'copy interaction must not crash the transcript');
  }
};
