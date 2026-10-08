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

  it('offers all three modes once opened, with the current one ticked', async () => {
    const wrapper = mountPicker();

    await wrapper.find('button').trigger('click');
    await flushPromises();

    const rows = wrapper.findAll('button.row-button');
    expect(rows.map(r => r.text())).toEqual(['变更前确认', '自动编辑', '完全访问']);
    expect(rows[1]!.classes()).toContain('bg-theme-main/10');
    expect(rows[1]!.findAll('i').length).toBe(2); // shield + tick
    expect(rows[0]!.findAll('i').length).toBe(1); // shield only
    expect(rows[2]!.findAll('i').length).toBe(1); // shield only
    // The icons match the spec: a question in a shield, a check in a shield, an
    // exclamation in a shield.
    expect(rows[0]!.find('i').classes()).toContain('shield-question-icon');
    expect(rows[1]!.find('i').classes()).toContain('shield-check-icon');
    expect(rows[2]!.find('i').classes()).toContain('shield-alert-icon');
  });

  it('switches to strict (confirm-before-changes) mode and moves the trigger glyph', async () => {
    const wrapper = mountPicker();
    await wrapper.find('button').trigger('click');
    await flushPromises();

    await wrapper.findAll('button.row-button')[0]!.trigger('click');
    await flushPromises();

    expect(modeApi.select).toHaveBeenCalledWith('sid-1', 'confirm_all');
    expect(wrapper.find('i.shield-question-icon').exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe('访问模式 · 变更前确认');
  });

  it('switches to full access and moves the trigger glyph', async () => {
    const wrapper = mountPicker();
    await wrapper.find('button').trigger('click');
    await flushPromises();

    await wrapper.findAll('button.row-button')[2]!.trigger('click');
    await flushPromises();

    expect(modeApi.select).toHaveBeenCalledWith('sid-1', 'full_access');
    expect(wrapper.find('i.shield-alert-icon').exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe('访问模式 · 完全访问');
  });

  it('sends the auto-edit choice back when picked again', async () => {
    modeApi.mode = 'full_access';
    const wrapper = mountPicker();
    await wrapper.find('button').trigger('click');
    await flushPromises();

    await wrapper.findAll('button.row-button')[1]!.trigger('click');

    expect(modeApi.select).toHaveBeenCalledWith('sid-1', 'auto_edit');
  });

  it('shows the strict mode the backend reported', async () => {
    modeApi.mode = 'confirm_all';
    const wrapper = mountPicker();

    expect(wrapper.find('i.shield-question-icon').exists()).toBe(true);
    expect(wrapper.find('button').attributes('title')).toBe('访问模式 · 变更前确认');
  });
});
