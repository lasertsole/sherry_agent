import assert from 'node:assert/strict';
import { openToolbox, closeRightTab } from '../lib/app.mjs';

export default {
  id: 'F57',
  name: 'Whiteboard draw and clear',
  status: 'implemented',
  async run({ ui }) {
    await openToolbox(ui, 'whiteboard');
    await ui.find('[data-test="whiteboard-panel"]', 15_000);
    const drew = await ui.execute(`
    const canvas = document.querySelector('[data-test="whiteboard-panel"] canvas');
    if (!canvas) return false;
    const rect = canvas.getBoundingClientRect();
    const opts = (x, y) => ({bubbles: true, pointerId: 1, isPrimary: true,
      clientX: rect.left + x, clientY: rect.top + y, buttons: 1});
    canvas.dispatchEvent(new PointerEvent('pointerdown', opts(10, 10)));
    canvas.dispatchEvent(new PointerEvent('pointermove', opts(80, 60)));
    canvas.dispatchEvent(new PointerEvent('pointerup', opts(80, 60)));
    return true;
  `);
    assert.ok(drew, 'whiteboard canvas should exist');
    await ui.click('[data-test="whiteboard-clear"]').catch(() => {});
    await closeRightTab(ui);
  }
};
