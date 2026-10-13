import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';
import type { ToastMessageOptions } from 'primevue/toast';
import {
  registerToastApi,
  toastInfo,
  toastSuccess,
  toastWarn,
  toastError,
  sendRequestErrorToast,
  _setClientFlag
} from '../toast';

/**
 * toast.ts decides whether it takes effect via the module-internal `meta.client` (= the module's own
 * import.meta). Vitest's import.meta is a module-level object that a test file cannot rewrite
 * across modules, so injection goes through the `_setClientFlag` helper. The test environment has no
 * Nuxt context: in safeT an undefined `useNuxtApp` throws a ReferenceError that gets swallowed by
 * try/catch and falls back to "return the key as-is" — exactly the degraded behavior under test.
 */

describe('toast 全局通知层', () => {
  let add: Mock<(message: ToastMessageOptions) => void>;

  beforeEach(() => {
    _setClientFlag(true);
    add = vi.fn<(message: ToastMessageOptions) => void>();
    registerToastApi({ add });
  });

  afterEach(() => {
    // Unregister and restore the client flag (module-level singleton state; avoids leaking into later cases in this file)
    registerToastApi(null);
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('未注册 api 时全部安全空操作（不抛错、不派发）', () => {
    registerToastApi(null); // explicitly return to the unregistered state
    expect(() => {
      toastInfo('s');
      toastSuccess('s');
      toastWarn('s');
      toastError('s', 'd');
      sendRequestErrorToast('/api/x');
    }).not.toThrow();
    expect(add).not.toHaveBeenCalled();
  });

  it('四个级别按默认 life 派发（info/success 3000，warn 5000，error 8000）', () => {
    toastInfo('i-summary', 'i-detail');
    toastSuccess('s-summary');
    toastWarn('w-summary');
    toastError('e-summary', 'e-detail');

    expect(add).toHaveBeenCalledTimes(4);
    expect(add).toHaveBeenNthCalledWith(1, {
      severity: 'info',
      summary: 'i-summary',
      detail: 'i-detail',
      life: 3000
    });
    expect(add).toHaveBeenNthCalledWith(2, {
      severity: 'success',
      summary: 's-summary',
      detail: undefined,
      life: 3000
    });
    expect(add).toHaveBeenNthCalledWith(3, {
      severity: 'warn',
      summary: 'w-summary',
      detail: undefined,
      life: 5000
    });
    expect(add).toHaveBeenNthCalledWith(4, {
      severity: 'error',
      summary: 'e-summary',
      detail: 'e-detail',
      life: 8000
    });
  });

  it('sendRequestErrorToast 使用 errors.requestFailed（safeT 回退原始 key）+ error 级别', () => {
    sendRequestErrorToast('/api/health (HTTP 500)');

    expect(add).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith({
      severity: 'error',
      // The test env has no Nuxt i18n, so safeT returns the key as-is (in production it is the translated text)
      summary: 'errors.requestFailed',
      detail: '/api/health (HTTP 500)',
      life: 8000
    });
  });

  it('registerToastApi(null) 注销后不再派发', () => {
    registerToastApi(null);
    toastError('after-unregister');
    expect(add).not.toHaveBeenCalled();
  });

  it('meta.client=false 时即使已注册也不派发（show 守卫）', () => {
    _setClientFlag(false);
    toastError('non-client');
    sendRequestErrorToast('also-non-client');
    expect(add).not.toHaveBeenCalled();
  });
});
