import { describe, it, expect } from 'vitest';
import { defineComponent } from 'vue';
import { mount } from '@vue/test-utils';
import type { SessionRecord } from '@/pages/home/type';
import type { SubagentRun } from '@/composables/bridge';
import {
  SESSION_ROW_ESTIMATE_PX,
  TASK_HEADER_ESTIMATE_PX,
  TASK_RUN_ESTIMATE_PX,
  buildSessionRows,
  buildTaskRows,
  taskRowEstimate,
  useVirtualRows
} from '../use-sidebar-virtual-rows';

const session = (id: string, title = id): SessionRecord => ({
  id,
  title,
  createTime: '2026-09-25 10:00'
});

const run = (runId: string): SubagentRun => ({ run_id: runId, depth: 1 }) as SubagentRun;

describe('buildSessionRows', () => {
  it('keys every session by id, display order preserved', () => {
    const rows = buildSessionRows([session('b'), session('a'), session('c')]);
    expect(rows.map(r => r.key)).toEqual(['s-b', 's-a', 's-c']);
    expect(rows[1]!.item.title).toBe('a');
  });

  it('keys stay stable across rebuilds (identity, not index)', () => {
    const first = buildSessionRows([session('x'), session('y')]);
    const second = buildSessionRows([session('y'), session('x')]);
    expect(second[0]!.key).toBe(first[1]!.key);
  });

  it('is empty for an empty list', () => {
    expect(buildSessionRows([])).toEqual([]);
  });
});

describe('buildTaskRows', () => {
  it('flattens each group into a header row followed by its run cards', () => {
    const rows = buildTaskRows([
      { sessionId: 'sid-1', runs: [run('r1'), run('r2')] },
      { sessionId: 'sid-2', runs: [run('r3')] }
    ]);
    expect(rows.map(r => r.key)).toEqual(['h-sid-1', 'r-r1', 'r-r2', 'h-sid-2', 'r-r3']);
    expect(rows.map(r => r.kind)).toEqual(['header', 'run', 'run', 'header', 'run']);
    expect(rows[0]!.runCount).toBe(2);
    expect(rows[4]!.run?.run_id).toBe('r3');
  });

  it('keeps a header for a group with no runs (header is the group identity)', () => {
    const rows = buildTaskRows([{ sessionId: 'empty', runs: [] }]);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.kind).toBe('header');
    expect(rows[0]!.runCount).toBe(0);
  });

  it('is empty when there are no groups', () => {
    expect(buildTaskRows([])).toEqual([]);
  });
});

describe('taskRowEstimate', () => {
  const rows = buildTaskRows([{ sessionId: 'sid', runs: [run('r1')] }]);

  it('estimates headers and cards separately', () => {
    expect(taskRowEstimate(rows, 0)).toBe(TASK_HEADER_ESTIMATE_PX);
    expect(taskRowEstimate(rows, 1)).toBe(TASK_RUN_ESTIMATE_PX);
  });

  it('falls back to a card estimate for an index past the end', () => {
    // The window can briefly read one index past the list while rows are replaced.
    expect(taskRowEstimate(rows, 99)).toBe(TASK_RUN_ESTIMATE_PX);
  });
});

type ListVm = {
  virtualRows: Array<{ index: number; key: string }>;
  totalSize: number;
  rowCount: number;
  rowAt: (index: number) => { key: string } | undefined;
  remeasure: () => void;
};

/**
 * Harness: the composable's contract is the row model plus the window it hands
 * the template. `@tanstack/vue-virtual` is stubbed in the shared test setup
 * (happy-dom has no layout), so this suite covers the model/window wiring and
 * the real geometry is verified against the browser.
 * @param rows
 * @returns The mounted harness wrapper (cast to the composable's surface).
 */
const mountHarness = (rows: SessionRecord[]) => {
  const wrapper = mount(
    defineComponent({
      props: { records: { type: Array as () => SessionRecord[], default: () => [] } },
      setup(props) {
        const rowsRef = computed(() => buildSessionRows(props.records));
        const scrollRef = useTemplateRef<HTMLDivElement>('scrollRef');
        const api = useVirtualRows(
          scrollRef,
          () => rowsRef.value,
          () => SESSION_ROW_ESTIMATE_PX
        );
        return { ...api, rowsRef, scrollRef };
      },
      template: `<div ref="scrollRef"><div v-for="v in virtualRows" :key="String(v.key)" :data-index="v.index" /></div>`
    }),
    { props: { records: rows } }
  );
  return wrapper as unknown as { vm: ListVm; findAll: (s: string) => unknown[] };
};

describe('useVirtualRows', () => {
  it('maps virtual indexes back to row models', () => {
    const wrapper = mountHarness([session('s1'), session('s2')]);
    const vm = wrapper.vm;
    expect(vm.rowAt(0)?.key).toBe('s-s1');
    expect(vm.rowAt(1)?.key).toBe('s-s2');
  });

  it('returns undefined for an index outside the list', () => {
    const vm = mountHarness([session('s1')]).vm;
    expect(vm.rowAt(5)).toBeUndefined();
  });

  it('renders one positioned row per model and drops them when the list empties', async () => {
    const wrapper = mountHarness([session('s1'), session('s2')]);
    expect(wrapper.findAll('[data-index]')).toHaveLength(2);
    const vm = wrapper.vm;
    await wrapper.setProps({ records: [] });
    expect(vm.virtualRows).toHaveLength(0);
  });

  it('re-measures safely before the container is attached and after', () => {
    const wrapper = mountHarness([]);
    expect(() => wrapper.vm.remeasure()).not.toThrow();
  });
});
