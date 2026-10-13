/**
 * The toolbar's terminal entry lists what this session is running and hands the
 * jump to the session page: a sub-agent row asks for that run's live context
 * view, a command row asks for its tool card in the chat (where the command's
 * output lives). The entry itself opens no panel.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive, ref } from 'vue';
import TasksButton from '@/pages/home/components/TasksButton.vue';

const state = vi.hoisted(() => ({
  loadTaskRuns: vi.fn(async () => {}),
  initTasks: vi.fn()
}));

// The entry owns the initial load (the chat's jump bar is gone), so both seams
// are mocked.
vi.mock('@/composables/subagent-sync', () => ({
  loadTaskRuns: state.loadTaskRuns,
  initTasks: state.initTasks
}));

const command = {
  id: 42,
  toolName: 'terminal',
  summary: 'npm run build',
  startedAtMs: Date.now() - 5000
};

const run = {
  run_id: 'run-1',
  task: 'check the logs',
  execution: { status: 'RUNNING', started_at: Date.now() - 9000 }
};

beforeEach(() => {
  state.loadTaskRuns.mockClear();
  state.initTasks.mockClear();
  vi.stubGlobal('useSubagentStore', () => reactive({ taskRuns: [run], focusedRunId: undefined, taskLoading: false }));
  vi.stubGlobal('useRunningCommandsStore', () => reactive({ commands: ref([command]) }));
});

const mountButton = () => mount(TasksButton, { props: { sessionId: 'sid-1' } });

/**
 * Open the popover (its trigger is the only button until it is open).
 * @param wrapper
 */
async function openPopover(wrapper: ReturnType<typeof mountButton>) {
  await wrapper.find('button').trigger('click');
  await flushPromises();
}

describe('TasksButton.vue (integration)', () => {
  it('lists the running sub-agents and commands with their elapsed times', async () => {
    const wrapper = mountButton();

    await openPopover(wrapper);

    const rows = wrapper.findAll('button.row-button');
    expect(rows.map(r => r.text())).toEqual([
      expect.stringContaining('check the logs'),
      expect.stringContaining('npm run build')
    ]);
    // Elapsed column, formatted as m:ss.
    expect(rows[0]!.text()).toMatch(/\d+:\d{2}/);
  });

  it('asks the session page for a sub-agent run\u2019s live context view', async () => {
    const wrapper = mountButton();
    await openPopover(wrapper);

    await wrapper.findAll('button.row-button')[0]!.trigger('click');

    expect(wrapper.emitted('focus')).toEqual([[{ kind: 'run', id: 'run-1' }]]);
    // The panel closes itself so the page it hands off to is visible.
    expect(wrapper.find('[data-test="toolbar-popover"]').exists()).toBe(false);
  });

  it('asks the session page to reveal a running command\u2019s tool card', async () => {
    const wrapper = mountButton();
    await openPopover(wrapper);

    await wrapper.findAll('button.row-button')[1]!.trigger('click');

    expect(wrapper.emitted('focus')).toEqual([[{ kind: 'command', id: 42 }]]);
  });

  it('shows the empty state when nothing is running', async () => {
    vi.stubGlobal('useSubagentStore', () => reactive({ taskRuns: [], focusedRunId: undefined }));
    vi.stubGlobal('useRunningCommandsStore', () => reactive({ commands: ref([]) }));
    const wrapper = mountButton();

    await openPopover(wrapper);

    expect(wrapper.findAll('button.row-button')).toHaveLength(0);
    expect(wrapper.text()).toContain('暂无');
  });
});
