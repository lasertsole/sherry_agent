import assert from 'node:assert/strict';
import { newSession, sendMessage } from '../lib/app.mjs';

export default {
  id: 'F04',
  name: 'Tool call card and result',
  status: 'implemented',
  async run({ ui }) {
    await newSession(ui);
    await sendMessage(ui, '[e2e:tools] use a tool');
    // The tool row re-renders as the stream settles; read and click the last
    // row in one script so a stale WebDriver handle cannot lose the race.
    const started = Date.now();
    let text = '';
    while (Date.now() - started < 60_000) {
      const state = await ui.execute(`
        const rows = document.querySelectorAll('[data-message-role="tool"]');
        const folds = document.querySelectorAll('[data-test="turn-history-status"]');
        const lastTool = rows[rows.length - 1];
        const lastFold = folds[folds.length - 1];
        return {
          tool: lastTool ? lastTool.innerText : '',
          fold: lastFold ? lastFold.innerText : '',
          expanded: lastFold ? lastFold.getAttribute('aria-expanded') : null,
        };
      `);
      text = state.tool;
      if (text.includes('read_file')) break;
      // A settled turn hides its tool cards behind the fold; expand the newest
      // fold so the tool card is genuinely rendered before asserting on it.
      if (state.fold.includes('Tools executed') && state.expanded !== 'true') {
        await ui.execute(`
          const folds = document.querySelectorAll('[data-test="turn-history-status"]');
          const last = folds[folds.length - 1];
          if (last) last.click();
        `);
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    assert.match(text, /read_file/);
    // The tool card is visible while the turn streams; click it before the
    // settled-turn fold hides it, then assert the fixture read result.
    await ui.execute(`
      const rows = document.querySelectorAll('[data-message-role="tool"]');
      const last = rows[rows.length - 1];
      if (last) last.click();
    `);
    await ui.waitText('Tool result received from the real backend.', 60_000);
  }
};
