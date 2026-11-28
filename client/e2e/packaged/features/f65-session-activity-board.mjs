import { sleep } from '../lib/driver.mjs';
import { closeRightTab, ensureSession } from '../lib/app.mjs';

export default {
  id: 'F65',
  name: 'Session activity and taskflow board',
  status: 'implemented',
  async run({ ui }) {
    await ensureSession(ui);
    await ui.click('button[aria-label="Running background tasks"]');
    await ui.find('[data-test="tasks-open-activity"]', 10_000);
    await ui.click('[data-test="tasks-open-activity"]');
    await ui.find('[data-test="session-activity-panel"]', 15_000);
    // Taskflow is expanded by default; only toggle when a previous run left
    // it collapsed so this stub is order-independent.
    if (!(await ui.exists('[data-test="session-activity-open-board"]'))) {
      await ui.click('[data-test="session-activity-toggle-taskflow"]');
      await sleep(300);
    }
    await ui.find('[data-test="session-activity-open-board"]', 10_000);
    await ui.click('[data-test="session-activity-open-board"]');
    await ui.find('[data-test="taskflow-board"]', 15_000);
    await closeRightTab(ui);
    await closeRightTab(ui);
  }
};
