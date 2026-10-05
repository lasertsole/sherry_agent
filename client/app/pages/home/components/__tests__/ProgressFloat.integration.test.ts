/**
 * The floating plan-progress read-out.
 *
 * Contract: it is an overlay in the chat area's top-right, collapsed to a pill by
 * default (the panel is only rendered after a click), it is PERMANENT — it stays
 * on screen with no plan at all, the panel then carrying the empty state — and
 * every number it displays comes from the pushed payloads: a WebSocket frame
 * re-renders it without a reload.
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

  it('stays on screen with no plan work, and the panel says there is none', async () => {
    // The float used to disappear when neither half had data; the user asked for
    // it to be permanent (始终显示，哪怕没执行任务). It must never leave the screen.
    const wrapper = await mountFloat();

    expect(wrapper.find('[data-test="progress-float"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="progress-float-summary"]').text()).toContain('0/0');
    // No wave chip without waves — an idle pill must not read "wave 0/0".
    expect(wrapper.find('[data-test="progress-float-waves"]').exists()).toBe(false);

    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    const panel = wrapper.get('[data-test="progress-float-panel"]');
    expect(panel.get('[data-test="progress-float-empty"]').text()).toBe('暂无进行中的任务');
    // Empty state replaces the plan body — no header, no rows, no flows.
    expect(panel.find('[data-test="progress-float-header"]').exists()).toBe(false);
    expect(panel.findAll('[data-test="progress-todo"]')).toHaveLength(0);
    expect(panel.findAll('[data-test="progress-flow"]')).toHaveLength(0);
  });

  it('leaves the empty state and grows the plan when the first frame lands', async () => {
    const wrapper = await mountFloat();
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="progress-float-empty"]').exists()).toBe(true);

    handlers().taskflow!(taskflowFrame([makeFlow()]));
    await flushPromises();

    const panel = wrapper.get('[data-test="progress-float-panel"]');
    expect(panel.find('[data-test="progress-float-empty"]').exists()).toBe(false);
    expect(panel.get('[data-test="progress-float-header"]').text()).toContain('2/3');
    expect(panel.findAll('[data-test="progress-flow"]')).toHaveLength(1);
  });

  it('is translucent while collapsed, opaque when expanded, over a 0.3s transition', async () => {
    // Class contract (happy-dom computes no stylesheet): the pill carries the
    // alpha variant + the transition, and swaps to the solid background on expand;
    // the panel itself is solid (it only exists while expanded).
    const wrapper = await mountFloat();
    const pill = wrapper.get('[data-test="progress-float-trigger"]');

    expect(pill.classes()).toContain('bg-white/70');
    expect(pill.classes()).toContain('transition-colors');
    expect(pill.classes()).toContain('duration-300');

    await pill.trigger('click');
    await flushPromises();

    expect(pill.classes()).toContain('bg-white');
    expect(pill.classes()).not.toContain('bg-white/70');
    const panel = wrapper.get('[data-test="progress-float-panel"]');
    expect(panel.classes()).toContain('bg-white');
    expect(panel.classes()).not.toContain('bg-white/95');

    // Collapsing restores the translucent pill.
    await pill.trigger('click');
    await flushPromises();
    expect(pill.classes()).toContain('bg-white/70');
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

  it('expands on click into ONE box: checklist rows and waves under a single header', async () => {
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    handlers().todo!(todoFrame());
    await flushPromises();

    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    const panel = wrapper.get('[data-test="progress-float-panel"]');
    // One header for the whole plan — not one per source.
    expect(panel.findAll('[data-test="progress-float-header"]')).toHaveLength(1);
    // 1 completed + 1 in-progress todo, plus the flow's 2/3.
    expect(panel.get('[data-test="progress-float-header"]').text()).toContain('3/5');
    expect(panel.get('[data-test="progress-float-todos"]').text()).toContain('跑测试');
    expect(panel.findAll('[data-test="progress-todo"]')).toHaveLength(2);
    expect(panel.findAll('[data-test="progress-flow"]')).toHaveLength(1);
    expect(panel.findAll('[data-test="progress-wave"]')).toHaveLength(2);
    expect(panel.findAll('[data-test="progress-step"]')).toHaveLength(3);
    expect(panel.text()).toContain('第二波任务 C');
  });

  it('shows a flow-linked todo once — inside its wave, never as a second row', async () => {
    // The agent may mirror a TaskFlow step into the checklist (`flow_id` +
    // `step_id`). Both halves are the same plan item, so the box must not list
    // it twice ("任务计划 和 计划清单不是一个东西吗").
    const wrapper = await mountFloat();
    handlers().taskflow!(taskflowFrame([makeFlow()]));
    handlers().todo!({
      event: 'todo_updated',
      session_id: 'sid-1',
      content: {
        todos: [
          { content: '第二波任务 C', status: 'in_progress', priority: 'high', flow_id: 'flow-1', step_id: 'c' },
          { content: '独立的清单项', status: 'pending', priority: 'medium' }
        ]
      }
    });
    await flushPromises();

    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();

    const panel = wrapper.get('[data-test="progress-float-panel"]');
    // Only the unlinked checklist row renders as a checklist row.
    expect(panel.findAll('[data-test="progress-todo"]')).toHaveLength(1);
    expect(panel.get('[data-test="progress-float-todos"]').text()).toContain('独立的清单项');
    // The mirrored step still appears once, inside its wave.
    expect(panel.findAll('[data-test="progress-step"]')).toHaveLength(3);
    // And it is counted once: 2 flow steps done + 1 unlinked todo out of 3 + 1.
    expect(panel.get('[data-test="progress-float-header"]').text()).toContain('2/4');
  });

  it('says "not started" while every step is merely ready', async () => {
    // A board nobody has begun must not read as "wave 1/1": that number belongs
    // to work in flight, and the user read it as exactly that.
    const wrapper = await mountFloat();
    handlers().taskflow!(
      taskflowFrame([
        makeFlow({
          done: 0,
          current_wave: 1,
          by_status: { ready: 3 },
          waves: [
            {
              index: 1,
              total: 3,
              done: 0,
              by_status: { ready: 3 },
              steps: [
                { step_id: 'a', task: 'A', status: 'ready' },
                { step_id: 'b', task: 'B', status: 'ready' },
                { step_id: 'c', task: 'C', status: 'ready' }
              ]
            }
          ],
          total: 3
        })
      ])
    );
    await flushPromises();

    expect(wrapper.get('[data-test="progress-float-summary"]').text()).toContain('0/3');
    expect(wrapper.get('[data-test="progress-float-waves"]').text()).toContain('未开始');

    // …and the panel carries no "current wave" tag either.
    await wrapper.get('[data-test="progress-float-trigger"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="progress-wave-current"]').exists()).toBe(false);
  });

  it('reports the wave once work is under way, and closure once every step settled', async () => {
    const wrapper = await mountFloat();
    // A dispatched step = a child agent is on it → the wave position is real.
    handlers().taskflow!(
      taskflowFrame([
        makeFlow({
          done: 0,
          current_wave: 2,
          by_status: { done: 2, dispatched: 1 },
          total: 3,
          waves: [
            makeFlow().waves[0]!,
            {
              index: 2,
              total: 1,
              done: 0,
              by_status: { dispatched: 1 },
              steps: [{ step_id: 'c', task: 'C', status: 'dispatched' }]
            }
          ]
        })
      ])
    );
    await flushPromises();
    expect(wrapper.get('[data-test="progress-float-waves"]').text()).toContain('2/2');

    // Every step settled but the flow still open → the number would be a lie.
    handlers().taskflow!(
      taskflowFrame([
        makeFlow({
          done: 3,
          current_wave: 0,
          total: 3,
          waves: [
            makeFlow().waves[0]!,
            {
              index: 2,
              total: 1,
              done: 1,
              by_status: { done: 1 },
              steps: [{ step_id: 'c', task: 'C', status: 'done' }]
            }
          ]
        })
      ])
    );
    await flushPromises();
    expect(wrapper.get('[data-test="progress-float-waves"]').text()).toContain('待收口');
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

  it('shows only the checklist when no flow is tracked', async () => {
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
