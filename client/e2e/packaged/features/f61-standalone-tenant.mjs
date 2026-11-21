import assert from 'node:assert/strict';

export default {
  id: 'F61',
  name: 'Standalone cell hides tenant switcher',
  status: 'implemented',
  async run({ ui }) {
    assert.ok(!(await ui.exists('[data-test="tenant-switcher"]')), 'no tenant switcher on a standalone cell');
  }
};
