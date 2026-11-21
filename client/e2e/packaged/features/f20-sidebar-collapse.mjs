export default {
  id: 'F20',
  name: 'Collapse and expand the left sidebar',
  status: 'implemented',
  async run({ ui }) {
    await ui.clickAriaPrefix('Collapse sidebar');
    await ui.waitGone('[data-test="session-sidebar"] [data-test="sidebar-title-row"]', 8_000);
    await ui.clickAriaPrefix('Expand sidebar');
    await ui.find('[data-test="sidebar-title-row"]', 8_000);
  }
};
