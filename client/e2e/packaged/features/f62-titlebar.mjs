export default {
  id: 'F62',
  name: 'Desktop title bar renders',
  status: 'implemented',
  async run({ ui }) {
    await ui.find('[data-test="titlebar"]', 10_000);
  }
};
