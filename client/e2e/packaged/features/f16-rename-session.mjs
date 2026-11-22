import assert from 'node:assert/strict';
import { Key, sleep } from '../lib/driver.mjs';
import { newSession, sessionRowSelector } from '../lib/app.mjs';

export default {
  id: 'F16',
  name: 'Rename a session inline',
  status: 'implemented',
  async run({ ui }) {
    const disposable = await newSession(ui);
    await ui.find(sessionRowSelector(disposable), 20_000);
    await ui.execute(`
      const row = document.querySelector(${JSON.stringify(sessionRowSelector(disposable))});
      const pencil = row && row.querySelector('button[aria-label="Rename session"]');
      if (pencil) pencil.click();
      return !!pencil;
    `);
    const input = await ui.find('input.p-inputtext[aria-label="Rename session"]', 10_000);
    await input.sendKeys('RenamedE2ESession');
    await input.sendKeys(Key.ENTER);
    await sleep(800);
    const started = Date.now();
    let text = '';
    while (Date.now() - started < 30_000) {
      text = await ui.driver.executeScript(
        "const row = document.querySelector(arguments[0]); return row ? (row.innerText || '') : '';",
        sessionRowSelector(disposable)
      );
      if (text.includes('RenamedE2ESession')) return;
      await sleep(500);
    }
    assert.fail(`row text did not update. disposable=${disposable} last=${JSON.stringify(text.slice(0, 60))}`);
  }
};
