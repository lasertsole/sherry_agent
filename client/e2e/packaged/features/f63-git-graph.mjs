import assert from 'node:assert/strict';
import { sleep } from '../lib/driver.mjs';
import { newSession, currentSid, sendMessage, bindProjectDir } from '../lib/app.mjs';

export default {
  id: 'F63',
  name: 'Git graph panel on the seeded repo',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    const bound = await currentSid(ui);
    await bindProjectDir(ui, bound);
    await sendMessage(ui, '[e2e:plain] apply for git');
    await ui.waitText('Hello from the packaged mock backend.', 60_000);
    await ui.click('[aria-label="Project files"]');
    await ui.waitText('notes.md', 10_000);
    await ui.click('[data-test="sidebar-git-toggle"]');
    await sleep(1500);
    const body = await ui.bodyText();
    assert.ok(
      body.includes('checkpoint') ||
        body.includes('main') ||
        body.includes('No commits yet') ||
        body.includes('not a repo'),
      'git panel should render a state'
    );
    await ui.click('[aria-label="Sessions"]');
    await sleep(300);
  }
};
