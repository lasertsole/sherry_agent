import { sleep } from '../lib/driver.mjs';
import { closeDialog } from '../lib/app.mjs';

export default {
  id: 'F17',
  name: 'Session filter search',
  status: 'implemented',
  async run({ ui }) {
    await ui.clickText('Filter');
    await sleep(500);
    await closeDialog(ui);
    await ui.clickText('Filter');
    await sleep(400);
  }
};
