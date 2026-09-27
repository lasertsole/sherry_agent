import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatTurnScrubber from '@/pages/home/components/ChatTurnScrubber.vue';

/**
 * Marks as ChatBox derives them: render order, one per recent user message.
 * @param rowIndex
 * @param turn
 * @param preview
 */
const mark = (rowIndex: number, turn: number, preview = `第 ${turn} 条`) => ({
  rowIndex,
  turn,
  preview
});

function mountScrubber(marks: ReturnType<typeof mark>[], activeIndex: number | null = null) {
  return mount(ChatTurnScrubber, { props: { marks, activeIndex } });
}

describe('ChatTurnScrubber.vue (integration)', () => {
  it('renders one mark per user turn, oldest first', () => {
    const wrapper = mountScrubber([mark(2, 1), mark(5, 2), mark(9, 3)]);

    const marks = wrapper.findAll('button');
    expect(marks).toHaveLength(3);
    expect(marks.map(m => m.attributes('aria-label'))).toEqual(['跳到第 1 轮', '跳到第 2 轮', '跳到第 3 轮']);
  });

  it('stays hidden while there is nothing to navigate', () => {
    expect(mountScrubber([]).find('nav').exists()).toBe(false);
    // A single turn has no second position to jump to.
    expect(
      mountScrubber([mark(0, 1)])
        .find('nav')
        .exists()
    ).toBe(false);
  });

  it('marks the turn being read and highlights it', () => {
    // `activeIndex` is a position within the marks (the list controller decides which
    // turn that is), so the second mark is the one being read here.
    const wrapper = mountScrubber([mark(2, 1), mark(5, 2)], 1);

    const marks = wrapper.findAll('button');
    expect(marks[1]!.attributes('aria-current')).toBe('true');
    // The bar itself is the button's inner span (the button is the 10px hit target).
    expect(marks[1]!.find('span').classes()).toContain('bg-theme-main');
    expect(marks[0]!.find('span').classes()).not.toContain('bg-theme-main');
  });

  it('keeps a comfortable step between the marks and a clickable hit target', () => {
    const wrapper = mountScrubber([mark(2, 1), mark(5, 2)]);

    // The rail spaces the steps out (gap-1.5) and each mark is a 10px-tall target
    // wrapping a thin bar — previously the 2px bar was both the mark and the target.
    expect(wrapper.find('nav').classes()).toContain('gap-1.5');
    // The rail hugs the marks (px-1) instead of padding them out…
    expect(wrapper.find('nav').classes()).toContain('px-1');
    const buttons = wrapper.findAll('button');
    expect(buttons.every(b => b.classes().includes('h-2.5'))).toBe(true);
    expect(buttons.every(b => b.findAll('span').length === 1)).toBe(true);
    // …and the bars themselves keep their width.
    expect(buttons[0]!.find('span').classes()).toContain('w-3.5');
  });

  it('uses the message snippet as the hover tooltip', () => {
    const wrapper = mountScrubber([mark(2, 1, '把右侧栏的最小宽度改成 280'), mark(5, 2, '')]);

    expect(wrapper.findAll('button')[0]!.attributes('title')).toBe('把右侧栏的最小宽度改成 280');
    // No snippet (an empty/attachment-only message) falls back to the turn label.
    expect(wrapper.findAll('button')[1]!.attributes('title')).toBe('跳到第 2 轮');
  });

  it('asks the list to scroll to the picked row', async () => {
    const wrapper = mountScrubber([mark(2, 1), mark(5, 2), mark(9, 3)]);

    await wrapper.findAll('button')[2]!.trigger('click');

    expect(wrapper.emitted('jump')).toEqual([[9]]);
  });
});
