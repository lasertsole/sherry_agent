/**
 * S2 expert editor: the right-column form. Pins the create/edit distinction
 * (name is immutable once the expert exists), the MCP chip editor, and the
 * save/delete hand-off to the registry store, which owns the mock backend.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createTestingPinia } from '@pinia/testing';
import { setActivePinia } from 'pinia';
import ExpertEditPanel from '../ExpertEditPanel.vue';
import { useExpertRegistryStore } from '@/stores/expert-registry';

/** PrimeVue is not installed in the test bootstrap; stub every widget used. */
const globalStubs = {
  Button: {
    name: 'Button',
    props: ['label', 'icon', 'severity', 'text', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}<slot /></button>'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue', 'placeholder', 'disabled'],
    emits: ['update:modelValue', 'keyup'],
    template:
      '<input :value="modelValue" :disabled="disabled" :placeholder="placeholder" ' +
      '@input="$emit(\'update:modelValue\', $event.target.value)" ' +
      '@keyup.enter="$emit(\'keyup\', $event)" />'
  },
  Textarea: {
    name: 'Textarea',
    props: ['modelValue', 'rows'],
    emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Select: {
    name: 'Select',
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<select :value="modelValue"><option v-for="o in options" :key="o" :value="o">{{ o }}</option></select>'
  },
  MultiSelect: {
    name: 'MultiSelect',
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<div data-test="stub-multiselect" />'
  },
  InputNumber: {
    name: 'InputNumber',
    props: ['modelValue', 'min', 'max', 'step', 'suffix'],
    emits: ['update:modelValue'],
    template: '<input type="number" :value="modelValue" />'
  }
};

/**
 * Seed the registry, then mount so the panel's watcher already has data.
 * @param payload
 * @param payload.expertName
 */
async function mountPanel(payload?: { expertName?: string | null }) {
  const store = useExpertRegistryStore();
  await store.loadAll();
  const wrapper = mount(ExpertEditPanel, {
    props: payload ? { payload } : {},
    global: { stubs: globalStubs }
  });
  await flushPromises();
  return { wrapper, store };
}

beforeEach(() => {
  setActivePinia(createTestingPinia({ stubActions: false, createSpy: vi.fn }));
});

describe('ExpertEditPanel.vue (S2)', () => {
  it('renders every form field in create mode, without a delete button', async () => {
    const { wrapper } = await mountPanel();
    for (const hook of [
      'expert-form-name',
      'expert-form-description',
      'expert-form-persona',
      'expert-form-model',
      'expert-form-thinking',
      'expert-form-role',
      'expert-form-skills',
      'expert-form-mcp',
      'expert-form-timeout'
    ]) {
      expect(wrapper.find(`[data-test="${hook}"]`).exists()).toBe(true);
    }
    expect(wrapper.find('[data-test="expert-save"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="expert-delete"]').exists()).toBe(false);
    // A blank draft is still "new": no delete, and the name stays editable.
    expect(wrapper.get('[data-test="expert-form-name"]').attributes('disabled')).toBeUndefined();
  });

  it('prefills an existing expert and locks its name', async () => {
    const { wrapper } = await mountPanel({ expertName: 'frontend-expert' });
    const name = wrapper.get('[data-test="expert-form-name"]');
    expect((name.element as HTMLInputElement).value).toBe('frontend-expert');
    expect(name.attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="expert-delete"]').exists()).toBe(true);
  });

  it('saves a new expert into the registry and emits saved', async () => {
    const { wrapper, store } = await mountPanel();
    const before = store.experts.length;

    await wrapper.get('[data-test="expert-form-name"]').setValue('qa-expert');
    await wrapper.get('[data-test="expert-form-description"]').setValue('Quality assurance');
    await wrapper.get('[data-test="expert-save"]').trigger('click');
    await flushPromises();

    expect(store.experts.length).toBe(before + 1);
    expect(store.byName.has('qa-expert')).toBe(true);
    expect(wrapper.emitted('saved')).toBeTruthy();
  });

  it('refuses to save while the name is blank', async () => {
    const { wrapper, store } = await mountPanel();
    const before = store.experts.length;

    await wrapper.get('[data-test="expert-save"]').trigger('click');
    await flushPromises();

    expect(store.experts.length).toBe(before);
    expect(wrapper.emitted('saved')).toBeFalsy();
  });

  it('updates an existing expert instead of creating a second one', async () => {
    const { wrapper, store } = await mountPanel({ expertName: 'reviewer-expert' });
    const before = store.experts.length;

    await wrapper.get('[data-test="expert-form-description"]').setValue('Tightened review gate');
    await wrapper.get('[data-test="expert-save"]').trigger('click');
    await flushPromises();

    expect(store.experts.length).toBe(before);
    expect(store.byName.get('reviewer-expert')?.description).toBe('Tightened review gate');
  });

  it('deletes an existing expert', async () => {
    const { wrapper, store } = await mountPanel({ expertName: 'reviewer-expert' });

    await wrapper.get('[data-test="expert-delete"]').trigger('click');
    await flushPromises();

    expect(store.byName.has('reviewer-expert')).toBe(false);
    expect(wrapper.emitted('saved')).toBeTruthy();
  });

  it('adds and removes MCP server chips', async () => {
    const { wrapper } = await mountPanel();
    const input = wrapper.get('[data-test="expert-form-mcp"]');

    await input.setValue('filesystem');
    await input.trigger('keyup.enter');
    expect(wrapper.findAll('[data-test="expert-mcp-chip"]').length).toBe(1);
    expect(wrapper.get('[data-test="expert-mcp-chip"]').text()).toContain('filesystem');

    // The same server must not be added twice.
    await input.setValue('filesystem');
    await input.trigger('keyup.enter');
    expect(wrapper.findAll('[data-test="expert-mcp-chip"]').length).toBe(1);

    await wrapper.get('[data-test="expert-mcp-chip"] button').trigger('click');
    expect(wrapper.findAll('[data-test="expert-mcp-chip"]').length).toBe(0);
  });
});
