import { sleep } from '../lib/driver.mjs';
import { newSession, sessionRowSelector } from '../lib/app.mjs';

export default {
  id: 'F18',
  name: 'Batch delete sessions',
  status: 'implemented',
  async run({ ui }) {
    const disposable = await newSession(ui);
    // "Select All" toggles the header checkbox; the row's own checkbox is the
    // reliable programmatic path (the text span is not the control).
    await ui.execute(`
    const box = document.querySelector('${sessionRowSelector(disposable)} .p-checkbox input[type="checkbox"]')
      || document.querySelector('${sessionRowSelector(disposable)} .p-checkbox');
    if (!box) return false;
    box.click();
    return true;
  `);
    await sleep(600);
    await ui.clickTextDeep('Delete Conversations');
    await ui.clickTextDeep('Delete').catch(() => ui.clickText('Delete'));
    await ui.waitGone(sessionRowSelector(disposable), 15_000);
  }
};
