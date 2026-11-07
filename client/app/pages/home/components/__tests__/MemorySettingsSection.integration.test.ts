/**
 * Memory settings tab: the UI-editable caps are read from the memory DB,
 * saved back through `/memory/settings`, and the usage-aging pass is explicit.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import MemorySettingsSection from '@/pages/home/components/MemorySettingsSection.vue';

const bridge = vi.hoisted(() => ({
  readMemorySettings: vi.fn(),
  writeMemorySettings: vi.fn(),
  readMemoryStats: vi.fn(),
  consolidateMemory: vi.fn(),
  readConsolidationStatus: vi.fn()
}));

vi.mock('~/composables/bridge', () => bridge);
vi.mock('vue-router', async importOriginal => {
  const actual = await importOriginal<typeof import('vue-router')>();
  return { ...actual, useRoute: () => ({ params: { sid: 's1' } }) };
});

const settings = {
  memory_char_limit: 2200,
  user_char_limit: 1375,
  facts_char_limit: 1375,
  max_unused_days: 90,
  semantic_recall_enabled: false,
  semantic_recall_top_k: 5,
  semantic_recall_char_budget: 4000
};
const stats = {
  targets: {
    memory: { count: 1, chars: 10, avg_usage: 0, archived: 0, limit: 2200, percent: 1, file: 'MEMORY.md' },
    user: { count: 0, chars: 0, avg_usage: 0, archived: 0, limit: 1375, percent: 0, file: 'USER.md' },
    facts: { count: 0, chars: 0, avg_usage: 0, archived: 0, limit: 1375, percent: 0, file: 'FACTS.md' }
  },
  settings,
  total_entries: 1,
  total_chars: 10
};

const stubs = {
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputNumber: {
    props: ['modelValue', 'min', 'max', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<input class="num" type="number" :value="modelValue" :min="min" :max="max" :disabled="disabled" @input="$emit(\'update:modelValue\', Number($event.target.value))" />'
  },
  ToggleSwitch: {
    props: ['modelValue', 'disabled'],
    template: '<button class="switch" :disabled="disabled"></button>'
  }
};

describe('MemorySettingsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.readMemorySettings.mockResolvedValue(settings);
    bridge.readMemoryStats.mockResolvedValue(stats);
    bridge.writeMemorySettings.mockImplementation(async body => ({ ...settings, ...body }));
    bridge.consolidateMemory.mockResolvedValue({
      task_id: 't1',
      status: 'completed',
      result: { archived: 2, entries_after: 1 }
    });
    bridge.readConsolidationStatus.mockResolvedValue({ task_id: 't1', status: 'completed' });
  });

  it('shows the persisted limits and usage', async () => {
    const wrapper = mount(MemorySettingsSection, { global: { stubs } });
    await flushPromises();

    const inputs = wrapper.findAll('input.num');
    expect((inputs[0].element as HTMLInputElement).value).toBe('2200');
    expect((inputs[3].element as HTMLInputElement).value).toBe('90');
    expect(wrapper.text()).toContain('1 条');
  });

  it('saves only through the settings endpoint', async () => {
    const wrapper = mount(MemorySettingsSection, { global: { stubs } });
    await flushPromises();

    await wrapper.findAll('input.num')[0].setValue('3000');
    const save = wrapper.findAll('button').find(button => button.text() === '保存记忆设置');
    await save!.trigger('click');
    await flushPromises();

    expect(bridge.writeMemorySettings).toHaveBeenCalledWith(
      expect.objectContaining({ memory_char_limit: 3000, max_unused_days: 90 })
    );
  });

  it('runs the consolidation task and reports the result', async () => {
    const wrapper = mount(MemorySettingsSection, { global: { stubs } });
    await flushPromises();

    const consolidate = wrapper.findAll('button').find(button => button.text() === '立即整理');
    await consolidate!.trigger('click');
    await flushPromises();

    expect(bridge.consolidateMemory).toHaveBeenCalledWith('s1');
    expect(wrapper.text()).toContain('已整理：归档 2 条，剩余 1 条');
  });

  it('exposes the semantic-recall switch and its budget controls', async () => {
    const wrapper = mount(MemorySettingsSection, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('button.switch').exists()).toBe(true);
    expect(wrapper.text()).toContain('语义召回');
    const numbers = wrapper.findAll('input.num');
    expect((numbers[4].element as HTMLInputElement).disabled).toBe(true);
    expect((numbers[5].element as HTMLInputElement).disabled).toBe(true);
  });
});
