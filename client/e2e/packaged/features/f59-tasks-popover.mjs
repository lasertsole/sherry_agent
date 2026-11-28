import { closeDialog, ensureSession } from '../lib/app.mjs';

export default {
  id: 'F59',
  name: 'Tasks popover sections',
  status: 'implemented',
  async run({ ui }) {
    await ensureSession(ui);
    // The left nav item is also labelled "Tasks"; target the toolbar button
    // explicitly so this exercises the TasksButton popover, not the task board.
    await ui.click('button[aria-label="Running background tasks"]');
    await ui.find('[data-test="tasks-open-activity"]', 10_000);
    await closeDialog(ui);
  }
};
