import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';
import type { ToastMessageOptions } from 'primevue/toast';
import { defineComponent, h, ref } from 'vue';
import { mount } from '@vue/test-utils';
import { useErrorCaptured, type MsgRefType } from '../errorCaptured';
import { registerToastApi, _setClientFlag } from '../toast';

/**
 * Unit tests for the errorCaptured factory function.
 *
 * Core flow: a child component throws → the page-level onErrorCaptured catches it →
 *   1) normalize the error message (Error / string / circular-reference object)
 *   2) extract the component name (named / unnamed → 'unknown component')
 *   3) logUtil.e logging (import.meta.dev is falsy under vitest → goes through console.error)
 *   4) Toast (global toastError by default; uses MsgRefType.open when a msgRef is injected)
 *   5) return false stops propagation → app.config.errorHandler must not be called
 *
 * The test environment has no Nuxt context: vue-i18n is aliased to a stub via
 * vitest.config, so `useI18n().t` is an identity function →
 * toast.summary === 'errors.pageError' (the key is returned as-is).
 */

/** Child component that throws an Error (named) */
const BoomChild = defineComponent({
  name: 'BoomChild',
  setup() {
    throw new Error('boom-from-child');
  },
  render: () => null
});

/** Child component that throws a string */
const StringChild = defineComponent({
  name: 'StringChild',
  setup() {
    throw 'plain-string-error';
  },
  render: () => null
});

/** Child component that throws a circular-reference object */
const CircularChild = defineComponent({
  name: 'CircularChild',
  setup() {
    const circular: Record<string, unknown> = { code: 'E_LOOP' };
    circular.self = circular;
    throw circular;
  },
  render: () => null
});

/** Unnamed child component ($options.name is empty → 'unknown component') */
const AnonymousChild = defineComponent({
  setup() {
    throw new Error('anonymous-error');
  },
  render: () => null
});

/** Child component with only __name (simulates the compiled output of a `<script setup>` SFC) */
const SfcLikeChild = defineComponent({
  __name: 'SfcLikeProbe',
  setup() {
    throw new Error('sfc-like-error');
  },
  render: () => null
});

/** Page shell: invokes the factory function and renders the child component */
function makePage(child: ReturnType<typeof defineComponent>, msgRef?: ReturnType<typeof ref<MsgRefType | undefined>>) {
  return defineComponent({
    setup(_, { slots }) {
      useErrorCaptured(msgRef);
      return () => h('div', slots.default?.());
    }
  });
}

function mountWithChild(
  child: ReturnType<typeof defineComponent>,
  opts?: { errorHandler?: (...args: unknown[]) => void; msgRef?: ReturnType<typeof ref<MsgRefType | undefined>> }
) {
  const Page = makePage(child, opts?.msgRef);
  return mount(Page, {
    slots: { default: () => h(child) },
    global: opts?.errorHandler ? { config: { errorHandler: opts.errorHandler as never } } : undefined
  });
}

describe('useErrorCaptured 工厂函数', () => {
  let add: Mock<(message: ToastMessageOptions) => void>;
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    _setClientFlag(true);
    add = vi.fn<(message: ToastMessageOptions) => void>();
    registerToastApi({ add });
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    registerToastApi(null);
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('Error 子组件错误被捕获：toast 显示 message + 日志含组件名 + 不再向上传播', () => {
    const appHandler = vi.fn();
    mountWithChild(BoomChild, { errorHandler: appHandler });

    expect(add).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'error',
        summary: 'errors.pageError',
        detail: 'boom-from-child'
      })
    );
    // logUtil.e: `<component name> happened error...` + the normalized errMsg
    expect(consoleErrorSpy).toHaveBeenCalledWith('BoomChild happened error...', 'boom-from-child');
    // return false stops propagation: app.config.errorHandler is not triggered
    expect(appHandler).not.toHaveBeenCalled();
  });

  it('string 类型错误按原样标准化', () => {
    mountWithChild(StringChild);
    expect(add).toHaveBeenCalledWith(expect.objectContaining({ detail: 'plain-string-error' }));
  });

  it('循环引用对象回退为 String(err)，且记录序列化失败日志', () => {
    mountWithChild(CircularChild);
    expect(add).toHaveBeenCalledWith(expect.objectContaining({ detail: '[object Object]' }));
    expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining('Failed to serialize err'));
  });

  it('无名子组件日志记为 unknown component', () => {
    mountWithChild(AnonymousChild);
    expect(consoleErrorSpy).toHaveBeenCalledWith('unknown component happened error...', 'anonymous-error');
  });

  it('仅有 __name 的 SFC 风格子组件回退取 __name 作为组件名', () => {
    mountWithChild(SfcLikeChild);
    expect(consoleErrorSpy).toHaveBeenCalledWith('SfcLikeProbe happened error...', 'sfc-like-error');
  });

  it('注入自定义 msgRef 时走 MsgRefType.open（文档接口约定），不再触发全局 toast', () => {
    const open = vi.fn();
    const msgRef = ref<MsgRefType | undefined>({ open });
    const appHandler = vi.fn();

    mountWithChild(BoomChild, { errorHandler: appHandler, msgRef });

    expect(open).toHaveBeenCalledWith({
      message: 'boom-from-child',
      type: 'text',
      position: 'bottom',
      duration: 1500
    });
    expect(add).not.toHaveBeenCalled();
    expect(appHandler).not.toHaveBeenCalled();
  });
});
