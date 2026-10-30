/**
 * The background-command section of the toolbar's terminal popup: a VIEW over
 * the composable — it renders what it is handed, offers the stop control only on
 * a running row, and reports the refusal the backend sent.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

const i18n = vi.hoisted(() => ({
  t: (key: string, params?: Record<string, unknown>) => (params ? `${key} ${JSON.stringify(params)}` : key)
}));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import BackgroundCommandsSection from '../BackgroundCommandsSection.vue';
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

function mountSection(props: Record<string, unknown> = {}) {
  return mount(BackgroundCommandsSection, {
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

describe('BackgroundCommandsSection', () => {
  it('renders nothing without tasks', () => {
    const wrapper = mountSection({ tasks: [], runningCount: 0 });

    expect(wrapper.find('[data-test="background-commands"]').exists()).toBe(false);
  });

  it('lists every task with its status and the running/total count', () => {
    const wrapper = mountSection();

    expect(wrapper.findAll('[data-test="background-task-status"]').map(n => n.text())).toEqual([
      'backgroundTasks.statusRunning',
      'backgroundTasks.statusCompleted'
    ]);
    expect(wrapper.text()).toContain('sleep 1000');
    expect(wrapper.text()).toContain('backgroundTasks.runningCount {"running":1,"total":2}');
  });

  it('keeps the pid and exit code in the row tooltip (no column room in the popup)', () => {
    const wrapper = mountSection();

    const rows = wrapper.findAll('li[data-task-id]');
    expect(rows[0]!.find('span.font-mono').attributes('title')).toBe('sleep 1000 · pid 4242');
    expect(rows[1]!.find('span.font-mono').attributes('title')).toBe('pytest -q · pid 111 · exit 0');
  });

  it('offers the stop control only on a running row', () => {
    const wrapper = mountSection();

    const kills = wrapper.findAll('[data-test="background-task-kill"]');
    expect(kills).toHaveLength(1);
    expect(wrapper.findAll('[data-task-id="bg-1"]')[0]!.find('[data-test="background-task-kill"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-task-id="bg-2"]')[0]!.find('[data-test="background-task-kill"]').exists()).toBe(
      false
    );
  });

  it('forwards the stop click with the task id', async () => {
    const kill = vi.fn();
    const wrapper = mountSection({ kill });

    await wrapper.find('[data-test="background-task-kill"]').trigger('click');

    expect(kill).toHaveBeenCalledWith('bg-1');
  });

  it('disables the control while the stop is in flight', () => {
    const wrapper = mountSection({ killing: new Set(['bg-1']) });

    expect(wrapper.find('[data-test="background-task-kill"]').attributes('disabled')).toBeDefined();
  });

  it('shows the requested stop instead of a stale "running" chip', () => {
    const wrapper = mountSection({ killing: new Set(['bg-1']) });

    expect(wrapper.findAll('[data-test="background-task-status"]').map(n => n.text())).toEqual([
      'backgroundTasks.statusStopping',
      'backgroundTasks.statusCompleted'
    ]);
  });

  it('shows the refusal the backend reported', () => {
    const wrapper = mountSection({ killRefusal: { taskId: 'bg-1', reason: 'not_found_or_not_running' } });

    expect(wrapper.find('[data-test="background-task-kill-refused"]').text()).toBe('backgroundTasks.killRefused');
  });
});
