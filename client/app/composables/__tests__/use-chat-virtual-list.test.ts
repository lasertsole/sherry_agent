import { describe, it, expect, vi, beforeEach } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { mount, type VueWrapper } from '@vue/test-utils';
import type { MessageItem } from '@/pages/home/type';
import { CHAT_ROLE } from '@/types/chat-role';
import {
  buildChatVirtualRows,
  buildTurnMarks,
  headRowsToRelease,
  MAX_LOADED_ROWS,
  TURN_SCRUBBER_PAGE_SIZE,
  useChatVirtualList
} from '../use-chat-virtual-list';

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'x',
  id: 1,
  turn_num: 0,
  timestamp: 't',
  ...over
});

describe('buildChatVirtualRows', () => {
  it('maps each turn group to a keyed row, order preserved', () => {
    const groups = [
      [msg({ id: 10, role: CHAT_ROLE.USER })],
      [msg({ id: 11, role: CHAT_ROLE.AI }), msg({ id: 12, role: CHAT_ROLE.TOOL })]
    ];
    const rows = buildChatVirtualRows(groups);
    expect(rows.map(r => r.key)).toEqual(['g-10', 'g-11']);
    expect(rows[1]!.group).toHaveLength(2);
  });

  it('keys are stable across rebuilds (prepend stability contract)', () => {
    const groups = [[msg({ id: 7 })]];
    const first = buildChatVirtualRows(groups);
    const second = buildChatVirtualRows(groups);
    expect(first[0]!.key).toBe(second[0]!.key);
  });

  it('tolerates empty groups without throwing', () => {
    expect(buildChatVirtualRows([[]])[0]!.key).toBe('g-empty');
  });
});

describe('headRowsToRelease', () => {
  it('releases nothing while the loaded window fits the cap', () => {
    expect(headRowsToRelease(MAX_LOADED_ROWS, 0, 5000, MAX_LOADED_ROWS)).toBe(0);
    expect(headRowsToRelease(10, 0, 5000, MAX_LOADED_ROWS)).toBe(0);
  });

  it('releases the surplus once the new head sits above the viewport', () => {
    // 10 rows loaded, cap 8 → 2 may go; the new head starts at 200 while the
    // reader is 500px down, so both are out of sight.
    expect(headRowsToRelease(10, 200, 500, 8)).toBe(2);
    // …but not while the new head is still inside the viewport.
    expect(headRowsToRelease(10, 700, 500, 8)).toBe(0);
    // Exactly at the viewport top counts as out of sight.
    expect(headRowsToRelease(10, 500, 500, 8)).toBe(2);
  });
});

describe('buildTurnMarks', () => {
  const rows = (groups: MessageItem[][]) => buildChatVirtualRows(groups);

  it('marks every user turn and skips AI-only groups', () => {
    const marks = buildTurnMarks(
      rows([
        [msg({ id: 1, role: CHAT_ROLE.USER, turn_num: 1, content: '  第一条   消息  ' })],
        [msg({ id: 2, role: CHAT_ROLE.AI, turn_num: 1, content: '答' })],
        [msg({ id: 3, role: CHAT_ROLE.USER, turn_num: 2, content: '第二条' })]
      ])
    );

    expect(marks.map(m => m.rowIndex)).toEqual([0, 2]);
    expect(marks.map(m => m.turn)).toEqual([1, 2]);
    // The preview is collapsed to one line for the tooltip.
    expect(marks[0]!.preview).toBe('第一条 消息');
  });

  it('skips background-task carriers (USER rows that are not the user speaking)', () => {
    const carrier = msg({ id: 4, role: CHAT_ROLE.USER, turn_num: 3, origin: 'subagent_completion' });
    expect(buildTurnMarks(rows([[carrier]]))).toEqual([]);
  });

  it('marks every loaded user turn (the rail pages through them)', () => {
    const groups = Array.from({ length: TURN_SCRUBBER_PAGE_SIZE + 5 }, (_, i) => [
      msg({ id: i + 1, role: CHAT_ROLE.USER, turn_num: i + 1, content: `第 ${i + 1} 条` })
    ]);

    const marks = buildTurnMarks(rows(groups));

    // No cap any more: the scrubber pages the list instead of dropping its head.
    expect(marks).toHaveLength(TURN_SCRUBBER_PAGE_SIZE + 5);
    expect(marks[0]!.turn).toBe(1);
    expect(marks.at(-1)!.turn).toBe(TURN_SCRUBBER_PAGE_SIZE + 5);
  });
});

/**
 * Force deterministic geometry on the happy-dom element.
 * @param el
 * @param scrollHeight
 * @param clientHeight
 * @param scrollTop
 */
const setGeometry = (el: HTMLElement, scrollHeight: number, clientHeight: number, scrollTop: number) => {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
  el.scrollTop = scrollTop;
};

type ListVm = {
  scrollContainerRef: HTMLElement | null;
  showScrollBottom: boolean;
  virtualRows: Array<{ index: number; key: string }>;
  totalSize: number;
  rows: Array<{ key: string }>;
  scrollOffset: number;
  activeMarkIndex: number | null;
  turnMarks: Array<{ rowIndex: number; turn: number; preview: string }>;
  virtualizer: { scrollToEnd: () => void; isAtEnd: () => boolean; scrollToIndex: (i: number, o?: unknown) => void };
  scrollToRow: (index: number) => void;
  updateScrollBottomBtn: () => void;
};

const Harness = defineComponent({
  props: {
    groups: { type: Array as () => MessageItem[][], default: () => [] },
    messages: { type: Array as () => MessageItem[], default: () => [] }
  },
  setup(props) {
    return {
      ...useChatVirtualList(
        () => props.groups,
        () => props.messages
      )
    };
  },
  template: `<div ref="scrollContainerRef" @scroll="updateScrollBottomBtn"></div>`
});

describe('useChatVirtualList', () => {
  let wrapper: VueWrapper;
  let vm: ListVm;

  beforeEach(() => {
    wrapper = mount(Harness, { props: { groups: [], messages: [] } });
    vm = wrapper.vm as unknown as ListVm;
  });

  it('binds the container ref and starts with the scroll button hidden', () => {
    expect(vm.scrollContainerRef).toBeInstanceOf(HTMLElement);
    expect(vm.showScrollBottom).toBe(false);
  });

  it('exposes the row model matching the groups', async () => {
    const groups = [[msg({ id: 1 })], [msg({ id: 2, role: CHAT_ROLE.AI })]];
    await wrapper.setProps({ groups });
    expect(vm.rows.map(r => r.key)).toEqual(['g-1', 'g-2']);
  });

  it('fires onReachTop once per top crossing and re-arms after leaving', async () => {
    const reached: number[] = [];
    const scoped = mount(
      defineComponent({
        props: {
          groups: { type: Array as () => MessageItem[][], default: () => [] },
          messages: { type: Array as () => MessageItem[], default: () => [] }
        },
        setup(props) {
          return {
            ...useChatVirtualList(
              () => props.groups,
              () => props.messages,
              { onReachTop: () => reached.push(Date.now()) }
            )
          };
        },
        template: `<div ref="scrollContainerRef" @scroll="onScroll"></div>`
      })
    );
    const el = (scoped.vm as unknown as { scrollContainerRef: HTMLElement }).scrollContainerRef;
    setGeometry(el, 5000, 500, 0); // at the top
    await scoped.trigger('scroll');
    await scoped.trigger('scroll'); // still at the top: no duplicate fire
    expect(reached).toHaveLength(1);
    setGeometry(el, 5000, 500, 400); // left the trigger zone: re-arm
    await scoped.trigger('scroll');
    setGeometry(el, 5000, 500, 0); // top again: fires
    await scoped.trigger('scroll');
    expect(reached).toHaveLength(2);
  });

  it('hands the oldest out-of-view rows to the release callback', async () => {
    const released: number[][] = [];
    const groups = [
      [msg({ id: 1 })],
      [msg({ id: 2, role: CHAT_ROLE.AI })],
      [msg({ id: 3 })],
      [msg({ id: 4, role: CHAT_ROLE.AI })]
    ];
    const scoped = mount(
      defineComponent({
        props: { groups: { type: Array as () => MessageItem[][], default: () => [] } },
        setup(props2) {
          return {
            ...useChatVirtualList(
              () => props2.groups,
              () => props2.groups.flat(),
              { maxLoadedRows: 2, onReleaseHead: ids => released.push(ids) }
            )
          };
        },
        template: `<div ref="scrollContainerRef" @scroll="onScroll"></div>`
      }),
      { props: { groups } }
    );
    const vm = scoped.vm as unknown as {
      scrollContainerRef: HTMLElement;
      virtualizer: { isAtEnd: () => boolean };
    };
    // The shared stub reports "pinned to the end"; a reader mid-history is not.
    vm.virtualizer.isAtEnd = () => false;
    const el = vm.scrollContainerRef;
    setGeometry(el, 4000, 500, 900); // 4 rows × 100px, reader 900px down
    await scoped.trigger('scroll');

    // Cap 2 → the first two rows go, with the ids of the messages they held.
    expect(released).toEqual([[1, 2]]);
  });

  it('never releases rows while the reader is pinned to the newest messages', async () => {
    const released: number[][] = [];
    const scoped = mount(
      defineComponent({
        props: { groups: { type: Array as () => MessageItem[][], default: () => [] } },
        setup(props2) {
          return {
            ...useChatVirtualList(
              () => props2.groups,
              () => props2.groups.flat(),
              { maxLoadedRows: 2, onReleaseHead: ids => released.push(ids) }
            )
          };
        },
        template: `<div ref="scrollContainerRef" @scroll="onScroll"></div>`
      }),
      {
        props: {
          groups: [
            [msg({ id: 1 })],
            [msg({ id: 2, role: CHAT_ROLE.AI })],
            [msg({ id: 3 })],
            [msg({ id: 4, role: CHAT_ROLE.AI })]
          ]
        }
      }
    );
    const el = (scoped.vm as unknown as { scrollContainerRef: HTMLElement }).scrollContainerRef;
    setGeometry(el, 4000, 500, 900);
    await scoped.trigger('scroll');

    // The stub reports isAtEnd() = true: watching the newest messages, the head must
    // stay put even though the window is over the cap.
    expect(released).toEqual([]);
  });

  it('jumps to a row through the virtualizer with a top alignment', async () => {
    const spy = vi.spyOn(vm.virtualizer, 'scrollToIndex').mockImplementation(() => {});
    (vm as unknown as { scrollToRow: (index: number) => void }).scrollToRow(3);
    await nextTick();

    expect(spy).toHaveBeenCalledWith(3, { align: 'start' });
  });

  it('reports no active mark while the list is empty', () => {
    expect(vm.activeMarkIndex).toBeNull();
    expect(vm.turnMarks).toEqual([]);
  });

  it('marks the newest turn while pinned to the bottom, not the row at the top edge', async () => {
    const groups = [
      [msg({ id: 1, role: CHAT_ROLE.USER, turn_num: 1 })],
      [msg({ id: 2, role: CHAT_ROLE.AI, turn_num: 1 })],
      [msg({ id: 3, role: CHAT_ROLE.USER, turn_num: 2 })],
      [msg({ id: 4, role: CHAT_ROLE.AI, turn_num: 2 })]
    ];
    await wrapper.setProps({ groups });

    // The stub's isAtEnd() reports "pinned": the reader is on the newest turn even
    // though the viewport top still sits inside the previous one.
    expect(vm.turnMarks.map(m => m.turn)).toEqual([1, 2]);
    expect(vm.activeMarkIndex).toBe(1);
  });

  it('always scrolls to the end when a USER message is appended', async () => {
    const spy = vi.spyOn(vm.virtualizer, 'scrollToEnd');
    await wrapper.setProps({ messages: [msg({ id: 1, role: CHAT_ROLE.USER })] });
    await nextTick();
    expect(spy).toHaveBeenCalled();
  });

  it('does not force-scroll on non-user changes while scrolled far up', async () => {
    // 4500px away from the bottom: outside the 80px follow threshold.
    setGeometry(vm.scrollContainerRef!, 5000, 500, 0);
    const spy = vi.spyOn(vm.virtualizer, 'scrollToEnd');
    await wrapper.setProps({
      messages: [msg({ id: 1, role: CHAT_ROLE.AI, content: 'chunk' }), msg({ id: 2, role: CHAT_ROLE.AI })]
    });
    await nextTick();
    expect(spy).not.toHaveBeenCalled();
  });

  it('follows non-user changes while pinned near the bottom (streaming growth)', async () => {
    // 20px away: inside the threshold, so streaming keeps the tail visible.
    setGeometry(vm.scrollContainerRef!, 5000, 500, 4480);
    const spy = vi.spyOn(vm.virtualizer, 'scrollToEnd');
    await wrapper.setProps({
      messages: [msg({ id: 1, role: CHAT_ROLE.AI, content: 'chunk' }), msg({ id: 2, role: CHAT_ROLE.AI })]
    });
    await nextTick();
    expect(spy).toHaveBeenCalled();
  });
});
