/**
 * The cell switcher: hidden for one cell, visible for several, and a select
 * calls the store's switch path (which reloads the page in production).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import TenantSwitcher from '@/components/TenantSwitcher.vue';
import { useAuthStore } from '@/stores/auth';

const bridge = vi.hoisted(() => ({
  fetchTenants: vi.fn(),
  switchTenantRequest: vi.fn()
}));
vi.mock('~/composables/bridge/auth', () => bridge);

const stubs = {
  Select: {
    props: ['modelValue', 'options', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<select class="sel" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)">' +
      '<option v-for="option in options" :key="option.tenant_id" :value="option.tenant_id">{{ option.tenant_id }}</option>' +
      '</select>'
  }
};

const tenants = [
  { tenant_id: 'team-a', role: 'owner', cell_status: 'running', is_default: true },
  { tenant_id: 'team-b', role: 'member', cell_status: 'stopped', is_default: false }
];

async function mountSwitcher() {
  const wrapper = mount(TenantSwitcher, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('TenantSwitcher', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    vi.stubGlobal('useAuthStore', () => useAuthStore());
    Object.values(bridge).forEach(fn => fn.mockReset());
    bridge.fetchTenants.mockResolvedValue({ active: 'team-a', tenants });
    bridge.switchTenantRequest.mockResolvedValue(undefined);
  });

  it('renders nothing for a standalone cell or a single membership', async () => {
    bridge.fetchTenants.mockResolvedValue({ active: '', tenants: [] });
    expect((await mountSwitcher()).find('[data-test="tenant-switcher"]').exists()).toBe(false);

    bridge.fetchTenants.mockResolvedValue({ active: 'team-a', tenants: [tenants[0]] });
    expect((await mountSwitcher()).find('[data-test="tenant-switcher"]').exists()).toBe(false);
  });

  it('shows the active cell and switches when another is selected', async () => {
    const reload = vi.fn();
    vi.spyOn(window.location, 'reload').mockImplementation(reload);
    const wrapper = await mountSwitcher();
    const store = useAuthStore();

    expect(wrapper.get('[data-test="tenant-switcher"]').exists()).toBe(true);
    expect(store.tenants).toHaveLength(2);
    expect(store.activeTenant).toBe('team-a');

    await wrapper.get('select').setValue('team-b');
    await flushPromises();

    expect(bridge.switchTenantRequest).toHaveBeenCalledWith('team-b');
    expect(store.activeTenant).toBe('team-b');
    expect(reload).toHaveBeenCalled();
  });
});
