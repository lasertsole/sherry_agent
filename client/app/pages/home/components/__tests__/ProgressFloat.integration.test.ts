/**
 * The floating plan-progress read-out.
 *
 * Contract: it is an overlay in the chat area's top-right, collapsed to a pill by
 * default (the panel is only rendered after a click), it shows nothing at all when
 * neither half has data, and every number it displays comes from the pushed
 * payloads — a WebSocket frame re-renders it without a reload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import ProgressFloat from '@/pages/home/components/ProgressFloat.vue';
import type { FlowProgress } from '@/stores/taskflow';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mittMocks);

function makeFlow(overrides: Partial<FlowProgress> = {}): FlowProgress {
  return {
    flow_id: 'flow-1',
    status: 'running',
    description: '两波任务流',
    total: 3,
    done: 2,
    current_wave: 2,
    by_status: { done: 2, ready: 1 },
    waves: [
      {
        index: 1,
        total: 2,
        done: 2,
        by_status: { done: 2 },
        steps: [
          { step_id: 'a', task: '第一波任务 A', status: 'done' },
          { step_id: 'b', task: '第一波任务 B', status: 'done' }
        ]
      },
      {
        index: 2,
        total: 1,
        done: 0,
        by_status: { ready: 1 },
        steps: [{ step_id: 'c', task: '第二波任务 C', status: 'ready' }]
      }
    ],
    ...overrides
  };
}

function taskflowFrame(flows: FlowProgress[]) {
  return {
    event: 'taskflow_updated',
    session_id: 'sid-1',
    content: {
      flows,
      totals: {
        flows: flows.length,
        total: flows.reduce((sum, f) => sum + f.total, 0),
        done: flows.reduce((sum, f) => sum + f.done, 0),
        current_wave: flows[0]?.current_wave ?? 0,
        waves: flows.reduce((sum, f) => sum + f.waves.length, 0)
      }
    }
  };
}

function todoFrame() {
  return {
    event: 'todo_updated',
    session_id: 'sid-1',
    content: {
      todos: [
        { content: '写文档', status: 'completed', priority: 'medium' },
        { content: '跑测试', status: 'in_progress', priority: 'high' }
      ]
    }
  };
}

function handlers() {
  return {
    taskflow: mittMocks.on.mock.calls.find(c => c[0] === 'ws:taskflow_updated')?.[1] as
      ((payload: unknown) => void) | undefined,
    todo: mittMocks.on.mock.calls.find(c => c[0] === 'ws:todo_updated')?.[1] as ((payload: unknown) => void) | undefined
  };
}

async function mountFloat() {
  const wrapper = mount(ProgressFloat);
  await flushPromises();
  return wrapper;
}

describe('ProgressFloat', () => {
  beforeEach(() => {
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    setActivePinia(createTestingPinia({ stubActions: false }));
    // The component imports the real stores explicitly, so the active Pinia is the
    // only wiring this suite needs (the mitt mock is what feeds frames in).
  });

  it('renders nothing while no plan work exists', async () => {
    const wrapper = await mountFloat();

    expect(wrapper.find('[data-test="progress-float"]').exists()).toBe(false);
  });

  it('collapses to a pill by default, with the pushed numbers on it', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    await flushPromises();

    expect(wrapper.find('[data-test="progress-float"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="progress-float-panel"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="progress-float-summary"]').text()).toContain('2/3');
    expect(wrapper.get('[data-test="progress-float-waves"]').text()).toContain('2/2');
  });

  it('expands on click and lists both halves: todos and waves', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    handlers().todo!(todoFrame());
    await flushPromises();

    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    const panel = wrapper.get('[data-test="progress-float-panel"]');
    expect(panel.get('[data-test="progress-float-todos"]').text()).toContain('跑测试');
    expect(panel.findAll('[data-test="progress-todo"]')).toHaveLength(2);
    expect(panel.findAll('[data-test="progress-flow"]')).toHaveLength(1);
    expect(panel.findAll('[data-test="progress-wave"]')).toHaveLength(2);
    expect(panel.findAll('[data-test="progress-step"]')).toHaveLength(3);
    expect(panel.text()).toContain('第二波任务 C');
  });

  it('marks the first wave with open work as the current one', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    await flushPromises();
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    const waves = wrapper.findAll('[data-test="progress-wave"]');
    expect(waves[0]!.find('[data-test="progress-wave-current"]').exists()).toBe(false);
    expect(waves[1]!.find('[data-test="progress-wave-current"]').exists()).toBe(true);
  });

  it('flags a cyclic trailing wave', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(
      taskflowFrame([
        makeFlow({
          waves: [
            {
              index: 1,
              total: 2,
              done: 0,
              by_status: { ready: 2 },
              steps: [
                { step_id: 'a', task: 'A', status: 'ready' },
                { step_id: 'b', task: 'B', status: 'ready' }
              ],
              cyclic: true
            }
          ],
          total: 2,
          done: 0,
          current_wave: 1
        })
      ])
    );
    await flushPromises();
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="progress-wave-cyclic"]').exists()).toBe(true);
  });

  it('re-renders from a later push without remounting (live updates)', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    await flushPromises();
    expect(wrapper.get('[data-test="progress-float-summary"]').text()).toContain('2/3');

    // The last wave finishes: done 3/3, current_wave 0 (nothing left).
    handlers().taskflow!(
      taskflowFrame([
        makeFlow({
          done: 3,
          current_wave: 0,
          waves: [
            makeFlow().waves[0]!,
            {
              index: 2,
              total: 1,
              done: 1,
              by_status: { done: 1 },
              steps: [{ step_id: 'c', task: '第二波任务 C', status: 'done' }]
            }
          ]
        })
      ])
    );
    await flushPromises();

    expect(wrapper.get('[data-test="progress-float-summary"]').text()).toContain('3/3');
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="progress-wave-current"]').exists()).toBe(false);
  });

  it('shows only the todo half when no flow is tracked', async () => {
    const wrapper = await mountFloat();
    handlers().todo!(todoFrame());
    await flushPromises();

    expect(wrapper.find('[data-test="progress-float"]').exists()).toBe(true);
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="progress-float-todos"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test="progress-flow"]')).toHaveLength(0);
  });
});
