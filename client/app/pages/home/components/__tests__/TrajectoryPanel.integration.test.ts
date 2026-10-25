/**
 * TrajectoryPanel.vue (integration): the ledger timeline, search, folding
 * and the turn filter. The bridge is mocked; the route supplies the session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import TrajectoryPanel from '@/pages/home/components/TrajectoryPanel.vue';
import type { TrajectoryEvent } from '@/composables/bridge/session';

const bridge = vi.hoisted(() => ({
  fetchTrajectory: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);
vi.mock('vue-router', async importOriginal => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRoute: () => ({ params: { sid: 's1' } })
}));

const events: TrajectoryEvent[] = [
  {
    event_id: 't1:start',
    turn_id: 't1',
    seq: 0,
    ts: '2026-10-25T10:00:00',
    kind: 'turn_start',
    is_error: false,
    summary: 'turn_start',
    payload: { source: 'queue' }
  },
  {
    event_id: 't1:tool',
    turn_id: 't1',
    seq: 1,
    ts: '2026-10-25T10:00:01',
    kind: 'tool_start',
    is_error: false,
    summary: 'read_file',
    payload: {
      tool_name: 'read_file',
      args: { path: 'README.md' },
      result: 'contents',
      duration_ms: 1200
    }
  },
  {
    event_id: 't1:end',
    turn_id: 't1',
    seq: 0,
    ts: '2026-10-25T10:00:02',
    kind: 'turn_end',
    is_error: false,
    summary: 'turn_end',
    payload: { total_ms: 2000, ttfb_ms: 300 }
  },
  {
    event_id: 't2:retry',
    turn_id: 't2',
    seq: 0,
    ts: '2026-10-25T10:05:00',
    kind: 'model_retry',
    is_error: false,
    summary: 'model_retry',
    payload: { attempt: 2, max_attempts: 11, status: 'scheduled', reason: 'rate_limit', delay_ms: 1000 }
  }
];

const primevueStubs = {
  Button: {
    name: 'Button',
    props: ['label', 'icon', 'disabled'],
    emits: ['click'],
    template: '<button class="pb" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  Menu: { name: 'Menu', template: '<div class="menu"></div>' },
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" />'
  },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spinner"></div>' }
};

interface PanelVm {
  query: string;
  toolsCollapsed: boolean;
  lifecycleCollapsed: boolean;
  turnFilter: string;
  toggleSearch: () => void;
}

async function mountPanel() {
  const wrapper = mount(TrajectoryPanel, {
    global: { stubs: primevueStubs }
  });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  bridge.fetchTrajectory.mockReset().mockResolvedValue(events);
});

describe('TrajectoryPanel', () => {
  it('renders turns, summary and lifecycle/tool rows', async () => {
    const wrapper = await mountPanel();

    expect(bridge.fetchTrajectory).toHaveBeenCalledWith('s1', { limit: 500 });
    expect(wrapper.findAll('[data-turn-id]')).toHaveLength(2);
    expect(wrapper.text()).toContain('read_file');
    expect(wrapper.text()).toContain('4');
    expect(wrapper.text()).toContain('1.20s');
  });

  it('keeps tool rows collapsed until the row is opened', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.text()).not.toContain('"path": "README.md"');

    await wrapper.find('[data-event-id="t1:tool"] button').trigger('click');

    expect(wrapper.text()).toContain('"path": "README.md"');
    expect(wrapper.text()).toContain('contents');
  });

  it('filters all rows by a query and reports the match count', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.toggleSearch();
    await flushPromises();

    vm.query = 'rate_limit';
    await flushPromises();

    expect(wrapper.findAll('[data-turn-id]')).toHaveLength(1);
    expect(wrapper.text()).toContain('rate_limit');
    expect(wrapper.text()).not.toContain('read_file');
    expect(wrapper.text()).toContain('1/1');
  });

  it('filters to one turn and shows an empty state with no events', async () => {
    const wrapper = await mountPanel();
    (wrapper.vm as unknown as PanelVm).turnFilter = 't2';
    await flushPromises();

    expect(wrapper.findAll('[data-turn-id]')).toHaveLength(1);
    expect(wrapper.text()).not.toContain('read_file');

    bridge.fetchTrajectory.mockResolvedValue([]);
    await (wrapper.vm as unknown as { load: () => Promise<void> }).load();
    await flushPromises();

    expect(wrapper.findAll('[data-turn-id]')).toHaveLength(0);
  });
});
