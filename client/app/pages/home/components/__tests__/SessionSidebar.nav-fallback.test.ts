/**
 * S7 placeholder wiring: a nav item may be registered before its left-column
 * panel exists (that is the whole point of the registry). The shell must then
 * render the "not implemented" placeholder instead of an empty column, and must
 * swap to the real panel as soon as the item carries a component.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createTestingPinia } from '@pinia/testing';
import { setActivePinia } from 'pinia';
import { defineComponent, markRaw } from 'vue';
import SessionSidebar from '../SessionSidebar.vue';
import { useNavStore } from '@/stores/nav';

const StubPanel = markRaw(defineComponent({ name: 'StubPanel', template: '<div data-test="stub-panel" />' }));

function registerGhost(): void {
  useNavStore().register({ id: 'ghost', icon: 'pi-question', labelKey: 'nav.chat', scope: 'global' });
}

beforeEach(() => {
  setActivePinia(createTestingPinia({ stubActions: false, createSpy: vi.fn }));
  useNavStore().activate('chat');
});

describe('SessionSidebar.vue — nav placeholder (S7)', () => {
  it('shows the not-implemented placeholder for a component-less nav item', async () => {
    registerGhost();
    const wrapper = mount(SessionSidebar);
    await flushPromises();

    await wrapper.get('[data-test="nav-ghost"]').trigger('click');
    await flushPromises();

    expect(useNavStore().activeId).toBe('ghost');
    expect(wrapper.find('[data-test="not-implemented-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="stub-panel"]').exists()).toBe(false);
  });

  it('renders the real panel once the item carries a component', async () => {
    useNavStore().register({
      id: 'ghost',
      icon: 'pi-question',
      labelKey: 'nav.chat',
      scope: 'global',
      component: StubPanel
    });
    const wrapper = mount(SessionSidebar);
    await flushPromises();

    await wrapper.get('[data-test="nav-ghost"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="stub-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="not-implemented-panel"]').exists()).toBe(false);
  });
});
