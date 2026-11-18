import assert from 'node:assert/strict';

export default {
  id: 'F15',
  name: 'Backend connection health',
  status: 'implemented',
  async run({ ui }) {
    const body = await ui.bodyText();
    assert.ok(!body.includes('Backend service unreachable'));
    assert.ok(!body.includes('Network offline'));
    await ui.find('[contenteditable="true"][role="textbox"]', 15_000);
  }
};
