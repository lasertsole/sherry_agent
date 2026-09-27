import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import ContextUsageButton from '@/pages/home/components/ContextUsageButton.vue';

// The store is a Nuxt auto-import (like the toolbar's other stores): shadow it so
// the ring's geometry and the popover contents can be asserted directly.
let usageApi: {
  usage: { window: number; total: number; system: number; tools: number; messages: number } | null;
  refresh: ReturnType<typeof vi.fn>;
};

/**
 * Accounting payload with the parts the tests care about filled in.
 * @param over
 */
const usage = (over: Partial<NonNullable<typeof usageApi.usage>>) => ({
  window: 0,
  total: 0,
  system: 0,
  tools: 0,
  messages: 0,
  cache_hit_ratio: null as number | null,
  compress_ratio: 0.8,
  ...over
});

beforeEach(() => {
  usageApi = { usage: null, refresh: vi.fn(async () => {}) };
  vi.stubGlobal('useContextUsageStore', () => ({
    usageFor: () => usageApi.usage,
    refresh: usageApi.refresh
  }));
});

const mountButton = (usedTokens: number) => mount(ContextUsageButton, { props: { sessionId: 'sid-1', usedTokens } });

/**
 * The ring's dash offset, i.e. how much of the circle is still empty.
 * @param wrapper
 */
const ringOffset = (wrapper: ReturnType<typeof mountButton>): number => {
  const circles = wrapper.findAll('circle');
  return Number(circles[1]!.attributes('stroke-dashoffset'));
};

const RING_LENGTH = 2 * Math.PI * 9;

describe('ContextUsageButton.vue (integration, store mocked)', () => {
  it('reads the accounting on mount and when the turn changes', async () => {
    const wrapper = mountButton(0);

    expect(usageApi.refresh).toHaveBeenCalledWith('sid-1');
    usageApi.refresh.mockClear();
    await wrapper.setProps({ usedTokens: 12_000 });
    expect(usageApi.refresh).toHaveBeenCalledWith('sid-1');
  });

  it('fills the ring in proportion to the context window', () => {
    usageApi.usage = usage({ window: 100_000, total: 25_000, system: 5_000, tools: 5_000, messages: 15_000 });
    const wrapper = mountButton(0);

    // A quarter full: the arc keeps three quarters of its length hidden.
    expect(ringOffset(wrapper)).toBeCloseTo(RING_LENGTH * 0.75, 2);
  });

  it('falls back to the caller’s token count before the first read', () => {
    const wrapper = mountButton(50_000);

    // No window known yet → nothing to fill in.
    expect(ringOffset(wrapper)).toBeCloseTo(RING_LENGTH, 2);
  });

  it('warns once the window is nearly full', () => {
    usageApi.usage = usage({ window: 100_000, total: 95_000, system: 5_000, tools: 5_000, messages: 85_000 });
    const wrapper = mountButton(0);
    expect(wrapper.findAll('circle')[1]!.classes()).toContain('stroke-red-500');

    usageApi.usage = usage({ window: 100_000, total: 80_000, system: 5_000, tools: 5_000, messages: 70_000 });
    const amber = mountButton(0);
    expect(amber.findAll('circle')[1]!.classes()).toContain('stroke-amber-500');
  });

  it('shows the window, the share and the three parts in the popover', async () => {
    usageApi.usage = usage({ window: 128_000, total: 32_000, system: 8_000, tools: 4_000, messages: 20_000 });
    const wrapper = mountButton(32_000);

    expect(wrapper.find('[data-test="toolbar-popover"]').exists()).toBe(false);
    await wrapper.find('button').trigger('click');
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain('上下文容量');
    // 万 units for both sides of the ratio, plus the percentage.
    expect(text).toContain('3.2万 / 12.8万');
    expect(text).toContain('25.0%');
    // Legend rows: label + figures, no per-part bars.
    expect(text).toContain('消息');
    expect(text).toContain('系统提示词');
    expect(text).toContain('工具调用');
    // Legend figures are percentages only (the 万 pair stays in the header).
    expect(text).toContain('62.5%'); // messages
    expect(text).toContain('12.5%'); // tools
    expect(text).not.toContain('2.0万 ·');
    // Opening refreshes the numbers.
    expect(usageApi.refresh).toHaveBeenCalledWith('sid-1');
  });

  it('splits the bar into the legend’s three colours', async () => {
    usageApi.usage = usage({ window: 100_000, total: 50_000, system: 10_000, tools: 15_000, messages: 25_000 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const segments = wrapper.findAll('[data-test="stack-segment"]');
    // One segment per legend entry, in legend order, coloured like it.
    expect(segments).toHaveLength(3);
    expect(segments.map(s => s.classes()).flat()).toEqual(
      expect.arrayContaining(['bg-sky-500', 'bg-violet-500', 'bg-emerald-500'])
    );
    // Widths are shares of the WINDOW (25% + 10% + 15%), not of the prompt.
    expect(segments[0]!.attributes('style')).toContain('width: 25%');
    expect(segments[1]!.attributes('style')).toContain('width: 10%');
    expect(segments[2]!.attributes('style')).toContain('width: 15%');
    // The legend swatches carry the same colours, and the old per-part bars are gone.
    const swatches = wrapper
      .findAll('[data-test="legend-row"] span > span')
      .map(s => s.classes())
      .flat();
    expect(swatches).toEqual(expect.arrayContaining(['bg-sky-500', 'bg-violet-500', 'bg-emerald-500']));
    expect(wrapper.findAll('.h-1\\.5')).toHaveLength(0);
  });

  it('draws the compression threshold on the bar', async () => {
    usageApi.usage = usage({ window: 100_000, total: 90_000, compress_ratio: 0.8 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const threshold = wrapper.find('[role="separator"]');
    expect(threshold.exists()).toBe(true);
    expect(threshold.classes()).toContain('border-dashed');
    expect(threshold.classes()).toContain('border-red-500');
    expect(threshold.attributes('style')).toContain('left: 80%');

    // The same threshold is written in red next to the capacity label…
    const label = wrapper.findAll('span').find(s => s.text().startsWith('压缩阈值'));
    expect(label?.exists()).toBe(true);
    expect(label!.classes()).toContain('text-red-500');
    expect(label!.text()).toBe('压缩阈值 80%');

    // …and ticked on the ring itself, on the ring's own scale: 80% clockwise from
    // the top is 288°, and the arcs (not the svg) carry the -90° that moves their
    // start to the top.
    const tick = wrapper.find('line');
    expect(tick.exists()).toBe(true);
    expect(tick.classes()).toContain('stroke-red-500');
    expect(tick.attributes('transform')).toBe('rotate(288 12 12)');
    expect(wrapper.find('svg').classes()).not.toContain('-rotate-90');
    expect(wrapper.findAll('circle').every(c => c.attributes('transform') === 'rotate(-90 12 12)')).toBe(true);
  });

  it('reports the average cache hit rate, or a dash when unknown', async () => {
    usageApi.usage = usage({ window: 100_000, total: 10_000, cache_hit_ratio: 0.623 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('平均缓存命中率');
    expect(wrapper.text()).toContain('62.3%');

    usageApi.usage = usage({ window: 100_000, total: 10_000, cache_hit_ratio: null });
    const unknown = mountButton(0);
    await unknown.find('button').trigger('click');
    await flushPromises();
    expect(unknown.text()).toContain('—');
  });

  it('opens its breakdown above the toolbar trigger', async () => {
    usageApi.usage = usage({ window: 100_000, total: 10_000 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const panel = wrapper.find('[data-test="toolbar-popover"]');
    expect(panel.exists()).toBe(true);
    expect(panel.classes()).toContain('bottom-full');
    expect(panel.attributes('style')).toContain('left:');
    expect(panel.text()).toContain('上下文容量');
  });

  it('hides the threshold marks while the ratio is unknown', () => {
    usageApi.usage = usage({ window: 100_000, total: 10_000, compress_ratio: 0 });
    const wrapper = mountButton(0);

    // The ring itself never shows a tick at 0°, and the popover has no red label.
    expect(wrapper.find('line').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('压缩阈值');
  });

  it('names itself for screen readers', () => {
    const wrapper = mountButton(0);
    expect(wrapper.find('button').attributes('aria-label')).toBe('上下文占用');
  });
});
