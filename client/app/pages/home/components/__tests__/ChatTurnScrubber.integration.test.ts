import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { mount } from '@vue/test-utils';
import ChatTurnScrubber from '@/pages/home/components/ChatTurnScrubber.vue';
import { TURN_SCRUBBER_PAGE_SIZE } from '@/composables/use-chat-virtual-list';

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

  it('draws one page of marks and pages through the rest with the arrows', async () => {
    const marks = Array.from({ length: TURN_SCRUBBER_PAGE_SIZE + 6 }, (_, i) => mark(i * 2, i + 1));
    const wrapper = mountScrubber(marks);

    const arrows = () => wrapper.findAll('button').filter(b => (b.attributes('aria-label') || '').startsWith('更'));
    const bars = () => wrapper.findAll('button').filter(b => (b.attributes('aria-label') || '').startsWith('跳到'));
    expect(bars()).toHaveLength(TURN_SCRUBBER_PAGE_SIZE);

    const [older, newer] = arrows();
    expect(older!.attributes('disabled')).toBeDefined(); // first page
    await newer!.trigger('click');

    // The second page holds the remaining marks, and the newest page disables "newer".
    expect(bars()).toHaveLength(6);
    expect(bars()[0]!.attributes('aria-label')).toBe(`跳到第 ${TURN_SCRUBBER_PAGE_SIZE + 1} 轮`);
    expect(arrows()[1]!.attributes('disabled')).toBeDefined();

    await arrows()[0]!.trigger('click');
    expect(bars()[0]!.attributes('aria-label')).toBe('跳到第 1 轮');
  });

  it('follows the turn being read onto its page', async () => {
    const marks = Array.from({ length: TURN_SCRUBBER_PAGE_SIZE + 3 }, (_, i) => mark(i * 2, i + 1));
    const wrapper = mountScrubber(marks, 0);
    const barLabels = () =>
      wrapper
        .findAll('button')
        .filter(b => (b.attributes('aria-label') || '').startsWith('跳到'))
        .map(b => b.attributes('aria-label'));

    expect(barLabels()[0]).toBe('跳到第 1 轮');

    // Reading a turn from the second page brings that page into view.
    await wrapper.setProps({ activeIndex: TURN_SCRUBBER_PAGE_SIZE + 1 });
    expect(barLabels()[0]).toBe(`跳到第 ${TURN_SCRUBBER_PAGE_SIZE + 1} 轮`);
  });

  it('hides its own scrollbar when the rail outgrows its cap', () => {
    // A full page of marks can be taller than the rail's 72% cap on a short
    // window; the rail then scrolls, and its scrollbar would sit on top of the
    // chat text — the rail carries the class whose rules hide it.
    const wrapper = mountScrubber([mark(0, 1), mark(2, 2)]);

    const rail = wrapper.find('nav');
    expect(rail.classes()).toContain('scrubber-rail');
    expect(rail.classes()).toContain('overflow-y-auto');
    // The hiding rules live in the SFC's own styles (scoped), so the class must
    // not be a no-op: the source defines both the standard and the WebKit rule.
    const source = readFileSync('app/pages/home/components/ChatTurnScrubber.vue', 'utf8');
    expect(source).toContain('scrollbar-width: none');
    expect(source).toContain('.scrubber-rail::-webkit-scrollbar');
  });
});
