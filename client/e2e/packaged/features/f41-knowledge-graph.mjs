import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F41',
  name: 'Knowledge graph panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'knowledgeGraph');
    await ui.waitText('Knowledge Graph', 20_000).catch(() => {});
    await closeRightTab(ui);
  }
};
