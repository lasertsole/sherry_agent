/**
 * The background-command strip: a VIEW over the composable — it renders what it
 * is handed, offers the kill button only on a running row, and reports the
 * refusal the backend sent.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

const i18n = vi.hoisted(() => ({
  t: (key: string) => key
}));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import BackgroundTasksPanel from '../BackgroundTasksPanel.vue';
import type { BackgroundTaskItem } from '@/composables/use-background-tasks';

const running: BackgroundTaskItem = {
  taskId: 'bg-1',
  command: 'sleep 1000',
  status: 'running',
  pid: 4242,
  exitCode: null,
  reason: null
};
const done: BackgroundTaskItem = {
  taskId: 'bg-2',
  command: 'pytest -q',
  status: 'completed',
  pid: 111,
  exitCode: 0,
  reason: 'exited'
};

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(BackgroundTasksPanel, {
    props: {
      tasks: [running, done],
      runningCount: 1,
      killing: new Set<string>(),
      killRefusal: null,
      kill: vi.fn(),
      ...props
    }
  });
}

describe('BackgroundTasksPanel', () => {
  it('renders nothing without tasks', () => {
    const wrapper = mountPanel({ tasks: [], runningCount: 0 });

    expect(wrapper.find('[data-test="background-tasks"]').exists()).toBe(false);
  });

  it('lists every task with its status', () => {
    const wrapper = mountPanel();

    expect(wrapper.findAll('[data-test="background-task-status"]').map(n => n.text())).toEqual([
      'backgroundTasks.statusRunning',
      'backgroundTasks.statusCompleted'
    ]);
    expect(wrapper.text()).toContain('sleep 1000');
  });

  it('offers the kill button only on a running row', () => {
    const wrapper = mountPanel();

    const kills = wrapper.findAll('[data-test="background-task-kill"]');
    expect(kills).toHaveLength(1);
    expect(wrapper.findAll('[data-task-id="bg-1"]')[0]!.find('[data-test="background-task-kill"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-task-id="bg-2"]')[0]!.find('[data-test="background-task-kill"]').exists()).toBe(
      false
    );
  });

  it('forwards the kill click with the task id', async () => {
    const kill = vi.fn();
    const wrapper = mountPanel({ kill });

    await wrapper.find('[data-test="background-task-kill"]').trigger('click');

    expect(kill).toHaveBeenCalledWith('bg-1');
  });

  it('disables the button while the kill is in flight', () => {
    const wrapper = mountPanel({ killing: new Set(['bg-1']) });

    expect(wrapper.find('[data-test="background-task-kill"]').attributes('disabled')).toBeDefined();
  });

  it('shows the refusal the backend reported', () => {
    const wrapper = mountPanel({ killRefusal: { taskId: 'bg-1', reason: 'not_found_or_not_running' } });

    expect(wrapper.find('[data-test="background-task-kill-refused"]').text()).toBe('backgroundTasks.killRefused');
  });

  it('collapses and expands the row list', async () => {
    const wrapper = mountPanel();

    expect(wrapper.find('[data-test="background-task-rows"]').exists()).toBe(true);
    await wrapper.find('button[aria-expanded]').trigger('click');
    expect(wrapper.find('[data-test="background-task-rows"]').exists()).toBe(false);
  });
});
