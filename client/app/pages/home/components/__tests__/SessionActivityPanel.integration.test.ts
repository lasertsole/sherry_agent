/**
 * Session activity overview: the three collapsible sections, their empty states
 * and the jumps into the full board / a run's activity tab.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { reactive } from 'vue';
import { mount } from '@vue/test-utils';
import SessionActivityPanel from '@/pages/home/components/SessionActivityPanel.vue';

const stores = vi.hoisted(() => ({
  taskflow: null as Record<string, unknown> | null,
  subagent: null as Record<string, unknown> | null,
  commands: null as Record<string, unknown> | null,
  rightSidebar: { openTab: vi.fn() }
}));

vi.stubGlobal('useTaskflowStore', () => stores.taskflow);
vi.stubGlobal('useSubagentStore', () => stores.subagent);
vi.stubGlobal('useRunningCommandsStore', () => stores.commands);
vi.stubGlobal('useRightSidebarStore', () => stores.rightSidebar);

// Auto-imported composable → module mock (the store globals above are stubbed).
vi.mock('@/composables/useSubagentTasks', () => ({
  useSubagentTasks: () => ({
    roleLabel: () => 'researcher',
    runLabel: (run: { run_id: string }) => `run ${run.run_id}`,
    statusLabel: () => 'RUNNING'
  })
}));

const runningRun = {
  run_id: 'run-1',
  task: 'search',
  execution: { status: 'RUNNING', started_at: Date.now() - 5000, ended_at: null, outcome: null }
};
const finishedRun = {
  run_id: 'run-done',
  task: 'old',
  execution: { status: 'DONE', started_at: 0, ended_at: 1, outcome: { status: 'OK', error: null } }
};

function setStores(options: { flows?: unknown[]; totals?: unknown; runs?: unknown[]; commands?: unknown[] } = {}) {
  stores.taskflow = reactive({
    flows: options.flows ?? [],
    totals: options.totals ?? { flows: 0, total: 0, done: 0 }
  });
  stores.subagent = reactive({ taskRuns: options.runs ?? [] });
  stores.commands = reactive({ commands: options.commands ?? [] });
}

describe('SessionActivityPanel', () => {
  beforeEach(() => {
    stores.rightSidebar.openTab.mockClear();
    setStores();
  });

  it('renders three sections with their counts and empty states', () => {
    const wrapper = mount(SessionActivityPanel);

    expect(wrapper.get('[data-test="session-activity-toggle-taskflow"]').text()).toContain('0');
    expect(wrapper.get('[data-test="session-activity-toggle-subagents"]').text()).toContain('0');
    expect(wrapper.get('[data-test="session-activity-toggle-tools"]').text()).toContain('0');
    expect(wrapper.get('[data-test="session-activity-taskflow-empty"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="session-activity-subagents-empty"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="session-activity-tools-empty"]').exists()).toBe(true);
  });

  it('shows the active flows with a per-flow progress bar and opens the board', async () => {
    setStores({
      flows: [
        { flow_id: 'f1', status: 'RUNNING', description: 'Ship it', total: 4, done: 1, by_status: {}, steps: [] }
      ],
      totals: { flows: 1, total: 4, done: 1 }
    });
    const wrapper = mount(SessionActivityPanel);

    const flow = wrapper.get('[data-test="session-activity-flow-f1"]');
    expect(flow.text()).toContain('Ship it');
    expect(flow.text()).toContain('1/4');
    expect(flow.find('div.h-full').attributes('style')).toContain('width: 25%');

    await wrapper.get('[data-test="session-activity-open-board"]').trigger('click');
    expect(stores.rightSidebar.openTab).toHaveBeenCalledWith('taskFlowBoard');
  });

  it('lists only ACTIVE runs and opens one run activity tab', async () => {
    setStores({ runs: [runningRun, finishedRun] });
    const wrapper = mount(SessionActivityPanel);
    await wrapper.get('[data-test="session-activity-toggle-subagents"]').trigger('click'); // collapse
    await wrapper.get('[data-test="session-activity-toggle-subagents"]').trigger('click'); // expand

    expect(wrapper.find('[data-test="session-activity-run-run-1"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="session-activity-run-run-done"]').exists()).toBe(false);
    const row = wrapper.get('[data-test="session-activity-run-run-1"]');
    expect(row.text()).toContain('run run-1');
    expect(row.text()).toContain('researcher');

    await row.trigger('click');
    expect(stores.rightSidebar.openTab).toHaveBeenCalledWith('subagentActivity', { runId: 'run-1' });
  });

  it('lists the foreground tool calls', () => {
    setStores({ commands: [{ id: 42, summary: 'npm run build', startedAtMs: Date.now() - 3000 }] });
    const wrapper = mount(SessionActivityPanel);

    const row = wrapper.get('[data-test="session-activity-tool-42"]');
    expect(row.text()).toContain('npm run build');
    expect(row.text()).toMatch(/\d+:\d{2}/);
  });

  it('collapses a section without losing the others', async () => {
    setStores({ runs: [runningRun] });
    const wrapper = mount(SessionActivityPanel);

    await wrapper.get('[data-test="session-activity-toggle-subagents"]').trigger('click');
    expect(wrapper.find('[data-test="session-activity-run-run-1"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="session-activity-taskflow-empty"]').exists()).toBe(true);
  });
});
