/**
 * Memory panel: per-entry CRUD over the SQLite store.
 *
 * The old panel edited two whole Markdown files. It now lists rows with
 * provenance/usage, supports add/edit/archive/restore and toggles the archived
 * view — each action re-queries the backend, so the panel never renders an
 * optimistic state the store does not hold.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import MemoryPanel from '@/pages/home/components/MemoryPanel.vue';

const bridge = vi.hoisted(() => ({
  readMemoryEntries: vi.fn(),
  readMemoryStats: vi.fn(),
  createMemoryEntry: vi.fn(),
  updateMemoryEntry: vi.fn(),
  archiveMemoryEntry: vi.fn(),
  restoreMemoryEntry: vi.fn(),
  readMemorySettings: vi.fn(),
  writeMemorySettings: vi.fn(),
  searchMemoryEntries: vi.fn(),
  consolidateMemory: vi.fn()
}));

vi.mock('~/composables/bridge', () => bridge);

const stubs = {
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  Textarea: {
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<textarea class="ta" :value="modelValue" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  InputText: {
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<input class="input" :value="modelValue" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<select class="select" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>'
  },
  ToggleSwitch: {
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<button class="switch" :class="{ on: modelValue }" :disabled="disabled" @click="$emit(\'update:modelValue\', !modelValue)"></button>'
  },
  Tag: {
    props: ['value'],
    template: '<span class="tag">{{ value }}</span>'
  },
  ProgressSpinner: { template: '<div class="spinner" />' }
};

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    target: 'memory',
    content: 'Environment: Python 3.13',
    entry_type: 'project',
    origin_session: 'sess-a',
    created_at: 1_700_000_000_000,
    updated_at: null,
    last_used: null,
    usage_count: 2,
    char_count: 24,
    archived: false,
    archived_at: null,
    archived_reason: null,
    ...overrides
  };
}

function stats(overrides: Record<string, unknown> = {}) {
  const target = { count: 1, chars: 24, avg_usage: 2, archived: 0, limit: 2200, percent: 1, file: 'MEMORY.md' };
  return {
    targets: { memory: target, user: { ...target, count: 0, chars: 0 }, facts: { ...target, count: 0, chars: 0 } },
    settings: {
      memory_char_limit: 2200,
      user_char_limit: 1375,
      facts_char_limit: 1375,
      max_unused_days: 90,
      semantic_recall_enabled: false,
      semantic_recall_top_k: 5,
      semantic_recall_char_budget: 4000
    },
    total_entries: 1,
    total_chars: 24,
    ...overrides
  };
}

async function mountPanel() {
  const wrapper = mount(MemoryPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('MemoryPanel CRUD', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.readMemoryEntries.mockResolvedValue({
      entries: [entry()],
      total: 1,
      limit: 100,
      offset: 0,
      target: 'memory'
    });
    bridge.readMemoryStats.mockResolvedValue(stats());
    bridge.createMemoryEntry.mockResolvedValue(entry({ id: 2, content: 'new row' }));
    bridge.updateMemoryEntry.mockResolvedValue(entry({ content: 'edited row' }));
    bridge.archiveMemoryEntry.mockResolvedValue(entry({ archived: true, archived_reason: 'manual' }));
    bridge.restoreMemoryEntry.mockResolvedValue(entry());
  });

  it('loads the active target rows with provenance and usage', async () => {
    const wrapper = await mountPanel();

    expect(bridge.readMemoryEntries).toHaveBeenCalledWith(
      expect.objectContaining({ target: 'memory', includeArchived: false, sort: 'created' })
    );
    expect(wrapper.text()).toContain('Environment: Python 3.13');
    expect(wrapper.text()).toContain('使用 2 次');
    expect(wrapper.text()).toContain('sess-a');
    expect(wrapper.text()).toContain('1 条');
  });

  it('creates an entry through the backend and reloads', async () => {
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="memory-add"]').trigger('click');
    await wrapper.find('[data-test="memory-new-content"]').setValue('new row');
    const save = wrapper.findAll('button').find(button => button.text() === '保存');
    expect(save).toBeTruthy();
    await save!.trigger('click');
    await flushPromises();

    expect(bridge.createMemoryEntry).toHaveBeenCalledWith({
      target: 'memory',
      content: 'new row',
      entry_type: null
    });
    expect(bridge.readMemoryEntries).toHaveBeenCalledTimes(2);
  });

  it('edits an entry in place', async () => {
    const wrapper = await mountPanel();

    const edit = wrapper.findAll('button').find(button => button.text() === '编辑');
    await edit!.trigger('click');
    await wrapper.find('textarea.ta').setValue('edited row');
    const save = wrapper.findAll('button').find(button => button.text() === '保存');
    await save!.trigger('click');
    await flushPromises();

    expect(bridge.updateMemoryEntry).toHaveBeenCalledWith(1, { content: 'edited row' });
  });

  it('archives with the selected reason', async () => {
    const wrapper = await mountPanel();

    const reason = wrapper.findAll('select.select')[1];
    await reason.setValue('contradicted');
    const archive = wrapper.findAll('button').find(button => button.text() === '归档');
    await archive!.trigger('click');
    await flushPromises();

    expect(bridge.archiveMemoryEntry).toHaveBeenCalledWith(1, 'contradicted');
  });

  it('restores an archived row', async () => {
    bridge.readMemoryEntries.mockResolvedValue({
      entries: [entry({ archived: true, archived_reason: 'manual' })],
      total: 1,
      limit: 100,
      offset: 0,
      target: 'memory'
    });
    const wrapper = await mountPanel();

    const restore = wrapper.findAll('button').find(button => button.text() === '恢复');
    expect(restore).toBeTruthy();
    await restore!.trigger('click');
    await flushPromises();

    expect(bridge.restoreMemoryEntry).toHaveBeenCalledWith(1);
  });

  it('reloads with the archived filter when the toggle flips', async () => {
    const wrapper = await mountPanel();

    await wrapper.find('.switch').trigger('click');
    await flushPromises();

    expect(bridge.readMemoryEntries).toHaveBeenLastCalledWith(expect.objectContaining({ includeArchived: true }));
  });

  it('filters the loaded rows by the search box', async () => {
    bridge.readMemoryEntries.mockResolvedValue({
      entries: [entry(), entry({ id: 2, content: 'User: likes tea', origin_session: null })],
      total: 2,
      limit: 100,
      offset: 0,
      target: 'memory'
    });
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="memory-search"]').setValue('tea');
    await flushPromises();

    expect(wrapper.text()).toContain('User: likes tea');
    expect(wrapper.text()).not.toContain('Environment: Python 3.13');
  });
});
