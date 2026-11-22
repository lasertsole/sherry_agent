import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { newSession, currentSid, sendMessage, bindProjectDir } from '../lib/app.mjs';

export default {
  id: 'F21',
  name: 'Project files body and file tree',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    const bound = await currentSid(ui);
    await bindProjectDir(ui, bound);
    await sendMessage(ui, '[e2e:plain] apply the binding');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    // The toolbar folder button: "Project files" while the session list shows.
    await ui.click('[aria-label="Project files"]');
    await ui.waitText('notes.md', 10_000);
    const body = await ui.bodyText();
    assert.ok(body.includes('e2e-fixture.txt'), 'file tree should list the seeded fixture');
    await ui.click('[aria-label="Sessions"]');
    await sleep(400);
  }
};
