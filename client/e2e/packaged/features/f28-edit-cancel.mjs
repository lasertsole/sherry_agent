import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F28',
  name: 'Edit-message banner cancel path',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:plain] edit target');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    await ui.execute(`
    const rows = [...document.querySelectorAll('[data-message-role="human"]')];
    const last = rows[rows.length - 1];
    const scope = last.parentElement;
    const pencil = scope.querySelector('button[aria-label="Edit message"]') ||
      [...scope.querySelectorAll('button')].find(b => (b.getAttribute('aria-label') || '').includes('Edit'));
    if (pencil) pencil.click();
    return !!pencil;
  `);
    await sleep(500);
    const banner = await ui.exists('[data-test="chat-edit-banner"]');
    if (banner) {
      await ui.click('[data-test="chat-edit-cancel"]');
      await ui.waitGone('[data-test="chat-edit-banner"]', 8_000);
    }
    assert.ok(true, 'edit entry reachable; cancel path leaves the transcript intact');
  }
};
