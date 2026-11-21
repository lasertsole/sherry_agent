import assert from 'node:assert/strict';
import { openMenuTile, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F48',
  name: 'Persona / role editor panel',
  status: 'implemented',
  async run({ ui }) {
    await openMenuTile(ui, 'persona');
    await ui.waitText('Character Setup', 25_000).catch(() => {});
    const body = await ui.bodyText();
    assert.ok(
      body.includes('Character Setup') || body.includes('Persona') || body.includes('Soul'),
      'persona panel should render'
    );
    await closeRightTab(ui);
  }
};
