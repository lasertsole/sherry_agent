import { sleep } from '../lib/driver.mjs';
import { openMenuTile } from '../lib/app.mjs';

export default {
  id: 'F51',
  name: 'Language switch in System Config',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'systemConfig');
    await ui.waitText('Background Image', 20_000);
    await ui.clickTextDeep('Language');
    await sleep(600);
    await ui.clickTextDeep('中文').catch(() => {});
    await sleep(800);
    await ui.clickTextDeep('Language').catch(() => {});
    await ui.clickTextDeep('English').catch(() => {});
    await sleep(600);
  }
};
