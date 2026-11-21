import { sleep } from '../lib/driver.mjs';

export default {
  id: 'F35',
  name: 'Access mode switch',
  status: 'implemented',
  async run({ ui }) {
    for (const mode of ['Plan mode', 'Confirm changes', 'Auto edit']) {
      await ui.clickAriaPrefix('Access mode');
      await sleep(500);
      await ui.clickTextDeep(mode).catch(async () => {
        await ui.clickAriaPrefix('Access mode');
        await sleep(700);
        await ui.clickTextDeep(mode);
      });
    }
  }
};
