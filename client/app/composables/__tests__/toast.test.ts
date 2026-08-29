import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  registerToastApi,
  toastInfo,
  toastSuccess,
  toastWarn,
  toastError,
  sendRequestErrorToast,
  _setClientFlag,
} from '../toast';

/**
 * toast.ts 通过模块内部的 `meta.client`（= 该模块自己的 import.meta）决定是否生效。
 * Vitest 的 import.meta 是模块级对象，测试文件无法跨模块改写，故经 `_setClientFlag`
 * 助手注入。测试环境无 Nuxt 上下文：safeT 内的 `useNuxtApp` 未定义会抛 ReferenceError
 * 并被 try/catch 吞掉，回退为「原样返回 key」——这正是要验证的降级行为。
 */

describe('toast 全局通知层', () => {
  let add: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    _setClientFlag(true);
    add = vi.fn();
    registerToastApi({ add });
  });

  afterEach(() => {
    // 注销并还原 client 标志（模块级单例状态，避免泄漏到同文件后续用例）
    registerToastApi(null);
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('未注册 api 时全部安全空操作（不抛错、不派发）', () => {
    registerToastApi(null); // 显式回到未注册状态
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
      severity: 'info', summary: 'i-summary', detail: 'i-detail', life: 3000,
    });
    expect(add).toHaveBeenNthCalledWith(2, {
      severity: 'success', summary: 's-summary', detail: undefined, life: 3000,
    });
    expect(add).toHaveBeenNthCalledWith(3, {
      severity: 'warn', summary: 'w-summary', detail: undefined, life: 5000,
    });
    expect(add).toHaveBeenNthCalledWith(4, {
      severity: 'error', summary: 'e-summary', detail: 'e-detail', life: 8000,
    });
  });

  it('sendRequestErrorToast 使用 errors.requestFailed（safeT 回退原始 key）+ error 级别', () => {
    sendRequestErrorToast('/api/health (HTTP 500)');

    expect(add).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith({
      severity: 'error',
      // 测试环境无 Nuxt i18n，safeT 原样返回 key（生产环境为翻译后的文案）
      summary: 'errors.requestFailed',
      detail: '/api/health (HTTP 500)',
      life: 8000,
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
