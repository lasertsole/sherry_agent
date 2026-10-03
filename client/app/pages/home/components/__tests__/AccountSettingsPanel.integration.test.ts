/**
 * The account panel: three states, and the password proofs each action needs.
 *
 * Contract: no account → the setup form (which enables protection); protection
 * on → the change form plus the disable form (both need the current password —
 * the plan's R4); protection off with an account → the re-enable form. Failures
 * render in place, successes confirm in place.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import AccountSettingsPanel from '@/pages/home/components/AccountSettingsPanel.vue';
import { useAuthStore } from '@/stores/auth';

const bridge = vi.hoisted(() => ({
  fetchAuthStatus: vi.fn(),
  loginRequest: vi.fn(),
  logoutRequest: vi.fn(),
  fetchMe: vi.fn(),
  setupAccountRequest: vi.fn(),
  updateAccountRequest: vi.fn(),
  disableAuthRequest: vi.fn(),
  enableAuthRequest: vi.fn(),
  fetchWsTicket: vi.fn()
}));
vi.mock('~/composables/bridge/auth', () => bridge);

const stubs = {
  InputText: {
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Password: {
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template: '<input class="pw" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

async function mountPanel() {
  const wrapper = mount(AccountSettingsPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('AccountSettingsPanel', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    vi.stubGlobal('useAuthStore', () => useAuthStore());
    Object.values(bridge).forEach(fn => fn.mockReset());
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: false,
      hasAccount: false
    });
    bridge.fetchMe.mockResolvedValue(null);
  });

  it('offers the setup form while no account exists', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.get('[data-test="auth-status"]').text()).toContain('未启用');
    expect(wrapper.find('[data-test="auth-setup-hint"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="auth-setup-username"]').exists()).toBe(true);
  });

  it('creates the account through the setup form', async () => {
    bridge.setupAccountRequest.mockResolvedValue({ id: 1, username: 'admin' });
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="auth-setup-username"]').setValue('admin');
    await wrapper.get('[data-test="auth-setup-password"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-setup-confirm"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-setup-submit"]').trigger('click');
    await flushPromises();

    expect(bridge.setupAccountRequest).toHaveBeenCalledWith('admin', 'hunter2-long');
    expect(wrapper.get('[data-test="auth-success"]').text()).toContain('已启用');
  });

  it('keeps the setup button disabled until the confirmation matches', async () => {
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="auth-setup-username"]').setValue('admin');
    await wrapper.get('[data-test="auth-setup-password"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-setup-confirm"]').setValue('different-one');
    await flushPromises();

    expect(wrapper.get('[data-test="auth-setup-submit"]').attributes('disabled')).toBeDefined();
  });

  it('shows the change + disable forms while protection is on', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    const wrapper = await mountPanel();

    expect(wrapper.get('[data-test="auth-status"]').text()).toContain('已启用');
    expect(wrapper.get('[data-test="auth-current-user"]').text()).toContain('admin');
    expect(wrapper.find('[data-test="auth-new-username"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="auth-disable"]').exists()).toBe(true);
  });

  it('changes the password with the current one', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    bridge.updateAccountRequest.mockResolvedValue({ id: 1, username: 'admin' });
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="auth-new-password"]').setValue('brand-new-pass');
    await wrapper.get('[data-test="auth-current-password"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-save"]').trigger('click');
    await flushPromises();

    expect(bridge.updateAccountRequest).toHaveBeenCalledWith({
      password: 'hunter2-long',
      newUsername: undefined,
      newPassword: 'brand-new-pass'
    });
    expect(wrapper.get('[data-test="auth-success"]').text()).toContain('已保存');
  });

  it('renders a rejected password in place', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    bridge.updateAccountRequest.mockRejectedValue(
      Object.assign(new Error('the current password is incorrect'), { code: 'invalid_password' })
    );
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="auth-new-password"]').setValue('brand-new-pass');
    await wrapper.get('[data-test="auth-current-password"]').setValue('wrong');
    await wrapper.get('[data-test="auth-save"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="auth-error"]').text()).toContain('incorrect');
  });

  it('disables protection with the password, and can re-enable it', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    bridge.disableAuthRequest.mockResolvedValue(undefined as never);
    bridge.enableAuthRequest.mockResolvedValue(undefined as never);
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="auth-switch-password"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-disable"]').trigger('click');
    await flushPromises();

    expect(bridge.disableAuthRequest).toHaveBeenCalledWith('hunter2-long');
    expect(wrapper.get('[data-test="auth-status"]').text()).toContain('未启用');
    expect(wrapper.find('[data-test="auth-enable"]').exists()).toBe(true);

    await wrapper.get('[data-test="auth-switch-password"]').setValue('hunter2-long');
    await wrapper.get('[data-test="auth-enable"]').trigger('click');
    await flushPromises();

    expect(bridge.enableAuthRequest).toHaveBeenCalledWith('hunter2-long');
    expect(wrapper.get('[data-test="auth-status"]').text()).toContain('已启用');
  });
});
