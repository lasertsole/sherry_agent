import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
// The stub `vue-i18n` resolves the current locale's messages; drive it directly.
import { locale as i18nLocale } from 'vue-i18n';
import ContextUsageButton from '@/pages/home/components/ContextUsageButton.vue';

// The store is a Nuxt auto-import (like the toolbar's other stores): shadow it so
// the ring's geometry and the popover contents can be asserted directly.
let usageApi: {
  usage: {
    window: number;
    total: number;
    system: number;
    skills: number;
    tools: number;
    messages: number;
  } | null;
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
  skills: 0,
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

// The locale is a shared stub: a test that switches it hands it back.
afterEach(() => {
  i18nLocale.value = 'zh';
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
    usageApi.usage = usage({
      window: 100_000,
      total: 25_000,
      system: 5_000,
      skills: 1_000,
      tools: 5_000,
      messages: 14_000
    });
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
    usageApi.usage = usage({
      window: 100_000,
      total: 95_000,
      system: 5_000,
      skills: 0,
      tools: 5_000,
      messages: 85_000
    });
    const wrapper = mountButton(0);
    expect(wrapper.findAll('circle')[1]!.classes()).toContain('stroke-red-500');

    usageApi.usage = usage({
      window: 100_000,
      total: 80_000,
      system: 5_000,
      skills: 0,
      tools: 5_000,
      messages: 70_000
    });
    const amber = mountButton(0);
    expect(amber.findAll('circle')[1]!.classes()).toContain('stroke-amber-500');
  });

  it('shows the window, the share and the three parts in the popover', async () => {
    usageApi.usage = usage({
      window: 128_000,
      total: 32_000,
      system: 8_000,
      skills: 2_000,
      tools: 4_000,
      messages: 18_000
    });
    const wrapper = mountButton(32_000);

    expect(wrapper.find('[data-test="toolbar-popover"]').exists()).toBe(false);
    await wrapper.find('button').trigger('click');
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain('上下文容量');
    // 万 units for both sides of the ratio, plus the percentage.
    expect(text).toContain('3.2万 / 12.8万');
    expect(text).toContain('25.0%');
    // Legend rows: label + the part's own occupancy in 万 + its share of the prompt.
    expect(text).toContain('消息');
    expect(text).toContain('系统提示词');
    expect(text).toContain('技能索引');
    expect(text).toContain('工具列表');
    expect(text).toContain('1.8万 · 56.3%'); // messages (18k of 32k)
    expect(text).toContain('0.8万 · 25.0%'); // system prompt
    expect(text).toContain('0.2万 · 6.3%'); // skill index
    expect(text).toContain('0.4万 · 12.5%'); // tool list
    // Opening refreshes the numbers.
    expect(usageApi.refresh).toHaveBeenCalledWith('sid-1');
  });

  it('shows each part\u2019s own 万 even before the first reported prompt', async () => {
    // A fresh session reported no prompt yet: the tool list still occupies the
    // window, so the rows carry their 万 (and a dash instead of a bogus 0.0%).
    usageApi.usage = usage({ window: 128_000, total: 0, system: 11_000, skills: 3_000, tools: 8_000, messages: 0 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const rows = wrapper.findAll('[data-test="legend-row"]').map(r => r.text());
    expect(rows).toHaveLength(4);
    expect(rows[1]).toContain('1.1万 · —');
    expect(rows[2]).toContain('0.3万 · —'); // skill index
    expect(rows[3]).toContain('0.8万 · —');
    expect(rows.join(' ')).not.toContain('0.0%');
  });

  it('never renders a part that holds tokens as 0万', async () => {
    // Under one decimal of 万 the figure would read as "nothing here" — say so
    // instead, while a genuinely empty part stays a plain 0.
    usageApi.usage = usage({ window: 128_000, total: 5_000, system: 300, skills: 0, tools: 4_700, messages: 0 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const rows = wrapper.findAll('[data-test="legend-row"]').map(r => r.text());
    expect(rows[1]).toContain('<0.1万 · 6.0%'); // system prompt, 300 tokens
    expect(rows[0]).toContain('0万'); // messages, none
    expect(rows.join(' ')).not.toContain('0.0万');
  });

  it('counts in the locale\u2019s own unit', async () => {
    // 万 is a CJK ten-thousand unit: English reads the same numbers in k, and
    // Korean writes 만. Same accounting, per-locale unit + scale.
    usageApi.usage = usage({
      window: 128_000,
      total: 32_000,
      system: 8_000,
      skills: 2_000,
      tools: 4_000,
      messages: 18_000
    });

    i18nLocale.value = 'en';
    const english = mountButton(0);
    await english.find('button').trigger('click');
    await flushPromises();
    expect(english.text()).toContain('32.0k / 128k');
    expect(english.text()).toContain('8.0k · 25.0%'); // system prompt, in k
    expect(english.text()).toContain('2.0k · 6.3%'); // skill index, in k
    expect(english.text()).toContain('Tool list');
    expect(english.text()).toContain('Skill index');

    i18nLocale.value = 'ko';
    const korean = mountButton(0);
    await korean.find('button').trigger('click');
    await flushPromises();
    expect(korean.text()).toContain('3.2만 / 12.8만');
    expect(korean.text()).toContain('도구 목록');
  });

  it('splits the bar into the legend’s three colours', async () => {
    usageApi.usage = usage({
      window: 100_000,
      total: 50_000,
      system: 10_000,
      skills: 5_000,
      tools: 15_000,
      messages: 20_000
    });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const segments = wrapper.findAll('[data-test="stack-segment"]');
    // One segment per legend entry, in legend order, coloured like it.
    expect(segments).toHaveLength(4);
    expect(segments.map(s => s.classes()).flat()).toEqual(
      expect.arrayContaining(['bg-sky-500', 'bg-violet-500', 'bg-amber-500', 'bg-emerald-500'])
    );
    // Widths are shares of the WINDOW (20% + 10% + 5% + 15%), not of the prompt.
    expect(segments[0]!.attributes('style')).toContain('width: 20%');
    expect(segments[1]!.attributes('style')).toContain('width: 10%');
    expect(segments[2]!.attributes('style')).toContain('width: 5%');
    expect(segments[3]!.attributes('style')).toContain('width: 15%');
    // The legend swatches carry the same colours, and the old per-part bars are gone.
    const swatches = wrapper
      .findAll('[data-test="legend-row"] span > span')
      .map(s => s.classes())
      .flat();
    expect(swatches).toEqual(expect.arrayContaining(['bg-sky-500', 'bg-violet-500', 'bg-amber-500', 'bg-emerald-500']));
    expect(wrapper.findAll('.h-1\\.5')).toHaveLength(0);
  });

  it('draws the compression threshold on the bar', async () => {
    usageApi.usage = usage({ window: 100_000, total: 90_000, skills: 0, compress_ratio: 0.8 });
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
    usageApi.usage = usage({ window: 100_000, total: 10_000, skills: 0, cache_hit_ratio: 0.623 });
    const wrapper = mountButton(0);

    await wrapper.find('button').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('平均缓存命中率');
    expect(wrapper.text()).toContain('62.3%');

    usageApi.usage = usage({ window: 100_000, total: 10_000, skills: 0, cache_hit_ratio: null });
    const unknown = mountButton(0);
    await unknown.find('button').trigger('click');
    await flushPromises();
    expect(unknown.text()).toContain('—');
  });

  it('opens its breakdown above the toolbar trigger', async () => {
    usageApi.usage = usage({ window: 100_000, total: 10_000, skills: 0 });
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
    usageApi.usage = usage({ window: 100_000, total: 10_000, skills: 0, compress_ratio: 0 });
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
