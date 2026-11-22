import { openMenuTile, closeRightTab, clickTab } from '../lib/app.mjs';

export default {
  id: 'F55',
  name: 'Model Config tab groups',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'systemConfig');
    await ui.waitText('Background Image', 20_000);
    await clickTab(ui, 'Model Config');
    // The Model Config tab's group cards render below the embedder card; the
    // card is the stable marker that the tab content fully rendered.
    await ui.find('[data-test="code-embedder"]', 30_000);
    await closeRightTab(ui);
  }
};
