/**
 * S3 global task board (left column). Pins the flow list, the status filter,
 * the empty state, the click hand-off (router + right-column board) and the
 * mount/unmount polling loop.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createTestingPinia } from '@pinia/testing';
import { setActivePinia } from 'pinia';
import TaskBoardNavPanel from '../nav/TaskBoardNavPanel.vue';
import { useTaskflowStore, type FlowProgress } from '@/stores/taskflow';
import { useRightSidebarStore } from '@/stores/right-sidebar';

const flow = (over: Partial<FlowProgress>): FlowProgress => ({
  flow_id: 'flow-1',
  status: 'running',
  description: 'Ship the thing',
  total: 4,
  done: 2,
  by_status: { running: 1, done: 1 },
  steps: [],
  ...over
});

const FLOWS: FlowProgress[] = [
  flow({ flow_id: 'flow-1', status: 'running', description: 'Ship the thing', session_id: 's1' }),
  flow({ flow_id: 'flow-2', status: 'done', description: 'Ship the other thing', done: 4, session_id: 's2' })
];

const fetchMock = vi.fn();
const push = vi.fn();

/** PrimeVue is not installed in the test bootstrap; stub what the panel uses. */
const globalStubs = {
  Select: {
    name: 'Select',
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: '<select :value="modelValue" />'
  }
};

async function mountPanel() {
  const wrapper = mount(TaskBoardNavPanel, { global: { stubs: globalStubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createTestingPinia({ stubActions: false, createSpy: vi.fn }));
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ flows: FLOWS });
  vi.stubGlobal('$fetch', fetchMock);
  push.mockReset();
  vi.stubGlobal('useRouter', () => ({ push }));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('TaskBoardNavPanel.vue (S3)', () => {
  it('loads every flow on mount and renders one card each', async () => {
    const wrapper = await mountPanel();
    const store = useTaskflowStore();

    expect(fetchMock).toHaveBeenCalledWith('/api/taskflow/flows');
    expect(store.globalFlows.length).toBe(2);
    expect(wrapper.findAll('[data-test="task-board-card"]').length).toBe(2);
    expect(wrapper.text()).toContain('Ship the thing');
    expect(wrapper.text()).toContain('2/4');
  });

  it('filters the list by status', async () => {
    const wrapper = await mountPanel();
    const store = useTaskflowStore();

    store.globalFilter = 'done';
    await flushPromises();

    const cards = wrapper.findAll('[data-test="task-board-card"]');
    expect(cards.length).toBe(1);
    expect(wrapper.text()).toContain('Ship the other thing');
    expect(wrapper.text()).not.toContain('Ship the thing');
  });

  it('shows the empty state when nothing matches', async () => {
    fetchMock.mockResolvedValue({ flows: [] });
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="task-board-card"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="task-board-empty"]').exists()).toBe(true);
  });

  it('opens the flow session and its board when a card is clicked', async () => {
    const wrapper = await mountPanel();
    const right = useRightSidebarStore();

    await wrapper.findAll('[data-test="task-board-card"]')[0]!.trigger('click');

    expect(push).toHaveBeenCalledWith('/home/s1');
    expect(right.tabs.some(tab => tab.kind === 'taskFlowBoard')).toBe(true);
  });

  it('polls while mounted and stops after unmount', async () => {
    vi.useFakeTimers();
    const wrapper = mount(TaskBoardNavPanel, { global: { stubs: globalStubs } });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(15000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
