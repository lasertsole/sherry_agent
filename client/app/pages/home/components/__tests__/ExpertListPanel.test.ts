/**
 * S2 expert list panel: the left-column registry browser. Pins card rendering,
 * keyword filtering, empty state and the create/edit hand-off to the right
 * sidebar. The backend is mocked inside the store (see expert-registry.test.ts).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createTestingPinia } from '@pinia/testing';
import { setActivePinia } from 'pinia';
import ExpertListPanel from '../nav/ExpertListPanel.vue';
import { useExpertRegistryStore } from '@/stores/expert-registry';
import { useRightSidebarStore } from '@/stores/right-sidebar';

const globalStubs = {
  Button: {
    name: 'Button',
    props: ['icon', 'size', 'ariaLabel'],
    emits: ['click'],
    template: '<button data-test="stub-button" @click="$emit(\'click\')"><slot /></button>'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<input data-test="stub-input" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  }
};

/** Seed the store's mock data, then mount so the first render already has rows. */
async function mountPanel() {
  const store = useExpertRegistryStore();
  await store.loadAll();
  const wrapper = mount(ExpertListPanel, { global: { stubs: globalStubs } });
  await flushPromises();
  return { wrapper, store };
}

beforeEach(() => {
  setActivePinia(createTestingPinia({ stubActions: false, createSpy: vi.fn }));
});

describe('ExpertListPanel.vue (S2)', () => {
  it('renders one card per expert with name and meta', async () => {
    const { wrapper, store } = await mountPanel();
    expect(store.experts.length).toBeGreaterThanOrEqual(2);
    expect(wrapper.findAll('[data-test="expert-card"]').length).toBe(store.experts.length);
    expect(wrapper.text()).toContain('frontend-expert');
  });

  it('filters experts by keyword and shows the empty state', async () => {
    const { wrapper } = await mountPanel();
    const input = wrapper.get('[data-test="filter-bar-keyword"]');
    await input.setValue('reviewer');
    expect(wrapper.text()).toContain('reviewer');
    expect(wrapper.text()).not.toContain('frontend-expert');

    await input.setValue('no-such-expert');
    expect(wrapper.get('[data-test="expert-empty"]').exists()).toBe(true);
  });

  it('create opens a blank expert tab; card click opens its editor', async () => {
    const { wrapper } = await mountPanel();
    const right = useRightSidebarStore();

    await wrapper.get('[data-test="expert-create"]').trigger('click');
    expect(right.tabs.some(tab => tab.kind === 'expertEdit' && tab.payload?.expertName === null)).toBe(true);

    await wrapper.findAll('[data-test="expert-card"]')[0]!.trigger('click');
    expect(right.tabs.some(tab => tab.kind === 'expertEdit' && typeof tab.payload?.expertName === 'string')).toBe(true);
  });
});
