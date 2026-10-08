/**
 * The login page: submit wiring, failure rendering, redirect target.
 *
 * The form never sees a token — the backend answers with HttpOnly cookies — so
 * what it owns is: disabled buttons until both fields are filled, the failure
 * text (never "which half was wrong"), and where a successful login goes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import LoginPage from '@/pages/login/index.vue';
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

const navigate = vi.hoisted(() => vi.fn(async () => {}));

const stubs = {
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Password: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="pw" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

async function mountLogin() {
  const wrapper = mount(LoginPage, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('login page', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    Object.values(bridge).forEach(fn => fn.mockReset());
    vi.stubGlobal('useAuthStore', () => useAuthStore());
    vi.stubGlobal('navigateTo', navigate);
    navigate.mockClear();
  });

  it('keeps the submit button disabled until both fields are filled', async () => {
    const wrapper = await mountLogin();

    expect(wrapper.get('[data-test="login-submit"]').attributes('disabled')).toBeDefined();

    await wrapper.get('[data-test="login-username"]').setValue('admin');
    await wrapper.get('[data-test="login-password"]').setValue('hunter2-long');
    await flushPromises();

    expect(wrapper.get('[data-test="login-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('signs in and navigates home', async () => {
    bridge.loginRequest.mockResolvedValue({ user: { id: 1, username: 'admin' }, expiresIn: 43200 });
    const wrapper = await mountLogin();

    await wrapper.get('[data-test="login-username"]').setValue('admin');
    await wrapper.get('[data-test="login-password"]').setValue('hunter2-long');
    await wrapper.get('form').trigger('submit');
    await flushPromises();

    expect(bridge.loginRequest).toHaveBeenCalledWith('admin', 'hunter2-long');
    expect(navigate).toHaveBeenCalledWith('/home');
  });

  it('renders a uniform failure and stays put', async () => {
    bridge.loginRequest.mockRejectedValue(
      Object.assign(new Error('invalid username or password'), { code: 'invalid_credentials' })
    );
    const wrapper = await mountLogin();

    await wrapper.get('[data-test="login-username"]').setValue('admin');
    await wrapper.get('[data-test="login-password"]').setValue('wrong');
    await wrapper.get('form').trigger('submit');
    await flushPromises();

    expect(wrapper.get('[data-test="login-error"]').text()).toContain('用户名或密码错误');
    expect(navigate).not.toHaveBeenCalled();
  });
});
