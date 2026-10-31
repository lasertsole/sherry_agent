/**
 * The background-command section of the toolbar's terminal popup: a VIEW over
 * the composable — it lists the commands still RUNNING (a settled command has
 * nothing left to watch or stop), offers the stop control on each, and reports
 * the refusal the backend sent.
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
      killing: new Set<string>(),
      killRefusal: null,
      kill: vi.fn(),
      ...props
    }
  });
}

describe('BackgroundCommandsSection', () => {
  it('renders nothing without tasks', () => {
    const wrapper = mountSection({ tasks: [] });

    expect(wrapper.find('[data-test="background-commands"]').exists()).toBe(false);
  });

  it('renders nothing when every command has settled', () => {
    const wrapper = mountSection({ tasks: [done, { ...done, taskId: 'bg-3', status: 'killed' }] });

    expect(wrapper.find('[data-test="background-commands"]').exists()).toBe(false);
  });

  it('lists the running command with its status and the running count', () => {
    const wrapper = mountSection();

    expect(wrapper.findAll('[data-test="background-task-status"]').map(n => n.text())).toEqual([
      'backgroundTasks.statusRunning'
    ]);
    expect(wrapper.text()).toContain('sleep 1000');
    expect(wrapper.text()).toContain('backgroundTasks.runningOnly {"count":1}');
  });

  it('hides a settled command even when it arrived after a running one', () => {
    const wrapper = mountSection({ tasks: [done] });

    expect(wrapper.text()).not.toContain('pytest -q');
    expect(wrapper.find('[data-test="background-task-kill"]').exists()).toBe(false);
  });

  it('keeps the pid in the row tooltip (no column room in the popup)', () => {
    const wrapper = mountSection();

    expect(wrapper.find('li[data-task-id="bg-1"] span.font-mono').attributes('title')).toBe('sleep 1000 · pid 4242');
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

    expect(wrapper.find('[data-test="background-task-status"]').text()).toBe('backgroundTasks.statusStopping');
  });

  it('shows the refusal the backend reported', () => {
    const wrapper = mountSection({ killRefusal: { taskId: 'bg-1', reason: 'not_found_or_not_running' } });

    expect(wrapper.find('[data-test="background-task-kill-refused"]').text()).toBe('backgroundTasks.killRefused');
  });
});
