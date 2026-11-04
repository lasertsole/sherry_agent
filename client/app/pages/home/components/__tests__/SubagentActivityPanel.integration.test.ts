/**
 * Agent activity panel (integration): header, the three tabs, summary fields,
 * the steer round-trip and the empty state.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { computed } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';
import SubagentActivityPanel from '@/pages/home/components/SubagentActivityPanel.vue';

const activity = vi.hoisted(() => ({
  state: {
    run: null as Record<string, unknown> | null,
    child: null as string | null,
    steerable: false
  },
  steer: vi.fn()
}));

vi.mock('@/composables/use-subagent-activity', () => ({
  useSubagentActivity: () => ({
    run: computed(() => activity.state.run),
    childSessionKey: computed(() => activity.state.child),
    isSteerable: computed(() => activity.state.steerable),
    steer: activity.steer
  })
}));

const toasts = vi.hoisted(() => ({ toastSuccess: vi.fn(), toastError: vi.fn() }));
vi.mock('~/composables/toast', () => toasts);

// The facade is an auto-imported composable (module-injected), so it must be
// mocked by module; the store is a plain Nuxt global and is stubbed below.
vi.mock('@/composables/useSubagentTasks', () => ({
  useSubagentTasks: () => ({
    badgeClass: () => 'badge-running',
    statusLabel: () => 'RUNNING',
    roleLabel: (run: { depth?: number }) => (run.depth === 1 ? 'researcher' : 'root')
  })
}));

const setActiveActivityRun = vi.hoisted(() => vi.fn());

/**
 * A running run record.
 * @param overrides
 */
function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    child_session_key: 'agent:subagent:child-1',
    requester_session_key: 'agent:main:session:parent',
    agent_id: 'agent-abc-123',
    depth: 1,
    role: 'researcher',
    task: 'search the codebase for SQL injection',
    label: 'search',
    execution: { status: 'RUNNING', started_at: Date.now() - 12_000, ended_at: null, outcome: null },
    completion: { required: false, result_text: null, captured_at: null },
    delivery: { status: 'NONE' },
    ...overrides
  };
}

const primeStubs = {
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  SubagentTrajectory: {
    name: 'SubagentTrajectory',
    props: ['sessionId'],
    template: '<div data-test="stub-trajectory" :data-session="sessionId" />'
  }
};

function mountPanel(payload: { runId?: string } | undefined) {
  return mount(SubagentActivityPanel, {
    props: { payload },
    global: { stubs: primeStubs }
  });
}

describe('SubagentActivityPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setActiveActivityRun.mockReset();
    vi.stubGlobal('useSubagentStore', () => ({ setActiveActivityRun }));
    activity.state = { run: makeRun(), child: 'agent:subagent:child-1', steerable: true };
    activity.steer.mockResolvedValue(true);
  });

  it('shows an empty state when the run is gone', async () => {
    activity.state = { run: null, child: null, steerable: false };
    const wrapper = mountPanel({ runId: 'gone' });

    expect(wrapper.find('[data-test="subagent-activity-empty"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="subagent-activity-header"]').exists()).toBe(false);
  });

  it('renders the header and points the live tab at the child session', async () => {
    const wrapper = mountPanel({ runId: 'run-1' });

    const header = wrapper.get('[data-test="subagent-activity-header"]');
    expect(header.text()).toContain('search');
    expect(header.text()).toContain('RUNNING');
    expect(header.text()).toContain('researcher');
    expect(header.text()).toContain('depth: 1');
    expect(header.text()).toContain('agent-abc-123');
    // Elapsed is a ticking m:ss clock.
    expect(wrapper.get('[data-test="subagent-activity-elapsed"]').text()).toMatch(/\d+:\d{2}/);

    const live = wrapper.get('[data-test="stub-trajectory"]');
    expect(live.attributes('data-session')).toBe('agent:subagent:child-1');
    // The shared store follows the open tab (and is cleared on unmount).
    expect(setActiveActivityRun).toHaveBeenCalledWith('run-1');
    wrapper.unmount();
    expect(setActiveActivityRun).toHaveBeenLastCalledWith(null);
  });

  it('lists the run summary fields and the bounded result text', async () => {
    const longResult = 'x'.repeat(4100);
    activity.state.run = makeRun({
      execution: {
        status: 'DONE',
        started_at: 1_700_000_000_000,
        ended_at: 1_700_000_012_000,
        outcome: { status: 'OK', error: null }
      },
      completion: { required: false, result_text: longResult, captured_at: null },
      ended_reason: 'completed',
      delivery: { status: 'DELIVERED' }
    });
    activity.state.steerable = false;
    const wrapper = mountPanel({ runId: 'run-1' });
    await wrapper.get('[data-test="subagent-activity-tab-summary"]').trigger('click');

    const summary = wrapper.get('[data-test="subagent-activity-summary"]');
    expect(summary.text()).toContain('OK');
    expect(summary.text()).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/);
    expect(summary.text()).toContain('completed');
    expect(summary.text()).toContain('DELIVERED');
    expect(summary.text()).toContain('search the codebase for SQL injection');
    const result = wrapper.get('[data-test="subagent-activity-result"]').text();
    expect(result.length).toBe(4001); // 4000 chars + the ellipsis
  });

  it('disables steering once the run is finished', async () => {
    activity.state.steerable = false;
    const wrapper = mountPanel({ runId: 'run-1' });
    await wrapper.get('[data-test="subagent-activity-tab-steer"]').trigger('click');

    expect(wrapper.get('[data-test="subagent-activity-steer-input"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="subagent-activity-steer-disabled"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="subagent-activity-steer-send"]').attributes('disabled')).toBeDefined();
  });

  it('sends the typed instructions and clears the box', async () => {
    const wrapper = mountPanel({ runId: 'run-1' });
    await wrapper.get('[data-test="subagent-activity-tab-steer"]').trigger('click');
    await wrapper.get('[data-test="subagent-activity-steer-input"]').setValue('focus on tests');
    await wrapper.get('[data-test="subagent-activity-steer-send"]').trigger('click');
    await flushPromises();

    expect(activity.steer).toHaveBeenCalledWith('focus on tests');
    expect((wrapper.get('[data-test="subagent-activity-steer-input"]').element as HTMLTextAreaElement).value).toBe('');
    expect(toasts.toastSuccess).toHaveBeenCalledTimes(1);
  });

  it('reports a refused steer as an error toast', async () => {
    activity.steer.mockResolvedValue(false);
    const wrapper = mountPanel({ runId: 'run-1' });
    await wrapper.get('[data-test="subagent-activity-tab-steer"]').trigger('click');
    await wrapper.get('[data-test="subagent-activity-steer-input"]').setValue('do it');
    await wrapper.get('[data-test="subagent-activity-steer-send"]').trigger('click');
    await flushPromises();

    expect(toasts.toastError).toHaveBeenCalledTimes(1);
  });
});
