/**
 * TaskFlow board panel: overall progress, swimlanes by status and the empty state.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { reactive } from 'vue';
import { mount } from '@vue/test-utils';
import TaskFlowBoardPanel from '@/pages/home/components/TaskFlowBoardPanel.vue';

const store = vi.hoisted(() => ({ state: null as Record<string, unknown> | null }));
vi.stubGlobal('useTaskflowStore', () => store.state);

/**
 * One step row.
 * @param stepId
 * @param status
 * @param task
 */
function step(stepId: string, status: string, task = `task ${stepId}`) {
  return { step_id: stepId, task, status };
}

/**
 * One flow with the given steps.
 * @param overrides
 */
function flow(overrides: Record<string, unknown> = {}) {
  return {
    flow_id: 'flow-1',
    status: 'RUNNING',
    description: 'Refactor the auth layer',
    total: 4,
    done: 1,
    by_status: { blocked: 1, ready: 1, dispatched: 1, done: 1 },
    steps: [step('s1', 'blocked'), step('s2', 'ready'), step('s3', 'dispatched'), step('s4', 'done')],
    ...overrides
  };
}

function setStore(flows: ReturnType<typeof flow>[], totals = { flows: 1, total: 4, done: 1 }) {
  store.state = reactive({
    flows,
    totals,
    percent: totals.total > 0 ? Math.round((totals.done / totals.total) * 100) : 0
  });
}

describe('TaskFlowBoardPanel', () => {
  beforeEach(() => setStore([flow()]));

  it('renders the overall progress and the per-flow header', () => {
    const wrapper = mount(TaskFlowBoardPanel);

    expect(wrapper.get('[data-test="taskboard-counts"]').text()).toBe('1/4');
    expect(wrapper.get('[data-test="taskboard-progress"]').attributes('style')).toContain('width: 25%');
    const board = wrapper.get('[data-test="taskboard-flow-flow-1"]');
    expect(board.text()).toContain('Refactor the auth layer');
    expect(board.text()).toContain('RUNNING');
  });

  it('groups the steps into one lane per status, in lifecycle order', () => {
    const wrapper = mount(TaskFlowBoardPanel);

    const lanes = wrapper.findAll('[data-test^="taskboard-lane-"]');
    expect(lanes.map(lane => lane.attributes('data-test'))).toEqual([
      'taskboard-lane-blocked',
      'taskboard-lane-ready',
      'taskboard-lane-dispatched',
      'taskboard-lane-done'
    ]);
    expect(wrapper.find('[data-test="taskboard-step-s3"]').exists()).toBe(true);
    // A dispatched step shows the running spinner.
    expect(wrapper.get('[data-test="taskboard-step-icon-s3"]').classes().join(' ')).toContain('pi-spin');
    expect(wrapper.get('[data-test="taskboard-step-icon-s4"]').classes().join(' ')).toContain('pi-check-circle');
  });

  it('tints failed steps and strikes out terminal ones', () => {
    setStore([
      flow({
        total: 3,
        done: 1,
        steps: [step('f1', 'failed'), step('k1', 'skipped'), step('c1', 'cancelled')]
      })
    ]);
    const wrapper = mount(TaskFlowBoardPanel);

    expect(wrapper.get('[data-test="taskboard-step-f1"]').classes().join(' ')).toContain('border-red');
    expect(wrapper.get('[data-test="taskboard-step-k1"]').text()).toContain('task k1');
    expect(wrapper.get('[data-test="taskboard-step-k1"]').html()).toContain('line-through');
    expect(wrapper.get('[data-test="taskboard-lane-failed"]').text()).toContain('f1');
  });

  it('renders an unknown status as its own trailing lane', () => {
    setStore([flow({ steps: [step('x1', 'paused')] })]);
    const wrapper = mount(TaskFlowBoardPanel);

    expect(wrapper.find('[data-test="taskboard-lane-paused"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="taskboard-step-x1"]').exists()).toBe(true);
  });

  it('shows the empty state without flows', () => {
    setStore([], { flows: 0, total: 0, done: 0 });
    const wrapper = mount(TaskFlowBoardPanel);

    expect(wrapper.find('[data-test="taskboard-empty"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test^="taskboard-flow-"]')).toHaveLength(0);
    expect(wrapper.get('[data-test="taskboard-progress"]').attributes('style')).toContain('width: 0%');
  });
});
