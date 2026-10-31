import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { reactive, ref } from 'vue';
import AccessModePicker from '@/pages/home/components/AccessModePicker.vue';

// The store is a Nuxt auto-import: a reactive double gives it the auto-unwrapped
// shape the component reads (`store.modeFor(...)`).
// `reactive` + `ref` mirror Pinia's auto-unwrapped state, so a change to the mode
// invalidates the component's computed instead of going unnoticed.
let modeApi: {
  mode: string;
  hydrate: ReturnType<typeof vi.fn>;
  select: ReturnType<typeof vi.fn>;
};
let state: { mode: string };

beforeEach(() => {
  const hydrate = vi.fn(async () => {});
  const select = vi.fn(async (_sid: string, mode: string) => {
    state.mode = mode;
  });
  state = reactive({ mode: ref('auto_edit') }) as { mode: string };
  modeApi = {
    get mode() {
      return state.mode;
    },
    set mode(value: string) {
      state.mode = value;
    },
    hydrate,
    select
  };
  vi.stubGlobal('useAccessModeStore', () => ({
    modeFor: () => state.mode,
    hydrate,
    select
  }));
});

const mountPicker = () => mount(AccessModePicker, { props: { sessionId: 'sid-1' } });

describe('AccessModePicker.vue (integration, store mocked)', () => {
  it('hydrates on mount and shows the auto-edit shield by default', () => {
    const wrapper = mountPicker();

    expect(modeApi.hydrate).toHaveBeenCalledWith('sid-1');
    expect(wrapper.find('i.shield-check-icon').exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe('访问模式 · 自动编辑');
  });

  it('offers all four modes once opened, with the current one ticked', async () => {
    const wrapper = mountPicker();

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const rows = wrapper.findAll('button.row-button');
    expect(rows.map(r => r.text())).toEqual(['计划模式', '变更前确认', '自动编辑', '完全访问']);
    expect(rows[2]!.classes()).toContain('bg-theme-main/10');
    expect(rows[2]!.findAll('i').length).toBe(2); // glyph + tick
    expect(rows[0]!.findAll('i').length).toBe(1); // glyph only
    expect(rows[1]!.findAll('i').length).toBe(1); // glyph only
    expect(rows[3]!.findAll('i').length).toBe(1); // glyph only
    // The glyphs match the spec: a list for plan mode, then a question / a
    // check / an exclamation in a shield.
    expect(rows[0]!.find('i').classes()).toContain('pi-list');
    expect(rows[1]!.find('i').classes()).toContain('shield-question-icon');
    expect(rows[2]!.find('i').classes()).toContain('shield-check-icon');
    expect(rows[3]!.find('i').classes()).toContain('shield-alert-icon');
  });

  // One parametrized family covers the four switch cases: same mount → open →
  // click-row flow, differing only in (start mode, row, mode value, glyph, title).
  it.each([
    {
      label: 'plan',
      startMode: 'auto_edit',
      row: 0,
      mode: 'plan',
      glyph: 'pi-list',
      title: '访问模式 · 计划模式'
    },
    {
      label: 'confirm-before-changes',
      startMode: 'auto_edit',
      row: 1,
      mode: 'confirm_all',
      glyph: 'shield-question-icon',
      title: '访问模式 · 变更前确认'
    },
    {
      label: 'auto-edit (re-picked from full access)',
      startMode: 'full_access',
      row: 2,
      mode: 'auto_edit',
      glyph: 'shield-check-icon',
      title: '访问模式 · 自动编辑'
    },
    {
      label: 'full access',
      startMode: 'auto_edit',
      row: 3,
      mode: 'full_access',
      glyph: 'shield-alert-icon',
      title: '访问模式 · 完全访问'
    }
  ])('switches to $label and moves the trigger glyph', async ({ startMode, row, mode, glyph, title }) => {
    modeApi.mode = startMode;
    const wrapper = mountPicker();
    await wrapper.find('button').trigger('click');
    await flushPromises();

    await wrapper.findAll('button.row-button')[row]!.trigger('click');
    await flushPromises();

    expect(modeApi.select).toHaveBeenCalledWith('sid-1', mode);
    expect(wrapper.find(`i.${glyph}`).exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe(title);
  });

  it('shows plan mode when the backend reports it (an agent-entered plan too)', async () => {
    modeApi.mode = 'plan';
    const wrapper = mountPicker();

    expect(wrapper.find('i.pi-list').exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe('访问模式 · 计划模式');
  });
});
