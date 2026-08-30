import type { Directive, DirectiveBinding } from "vue";

/**
 * v-debounce —— 事件防抖指令。
 *
 * 用法：
 *   v-debounce:click.500="handleSend"
 *     - arg（`:click`）：监听的 DOM 事件名，缺省为 `click`
 *     - modifiers（`.500`）：防抖延迟毫秒数，取第一个纯数字 modifier，缺省 300ms
 *   值为要防抖的回调；触发事件后静默 delay 毫秒才执行，期间重复触发会重新计时。
 *
 * 实现说明：
 *   - 事件监听器只绑定一次（mounted），通过稳定的包装函数转发到「最新回调」，
 *     因此 updated 时无需解绑/重绑（内联箭头函数每次渲染都是新引用）。
 *   - unmounted 时清除未触发的定时器并解绑，避免组件卸载后仍回调。
 */

/** 未显式指定延迟时的默认防抖毫秒数 */
const DEFAULT_DELAY = 300;

/** 挂在元素上的内部状态（用于解绑与清理） */
interface DebounceState {
  eventName: string;
  delay: number;
  /** 用户绑定的最新回调（updated 时原地替换） */
  latest: (...args: unknown[]) => void;
  /** 稳定的事件监听包装（mounted 时创建，unmounted 时解绑） */
  listener: (event: Event) => void;
  /** 当前未触发的定时器句柄 */
  timer: ReturnType<typeof setTimeout> | null;
}

/** 元素上存储状态的属性键（Symbol 避免与业务属性冲突） */
const STATE_KEY = Symbol("v-debounce-state");

/** 从 modifiers 中提取第一个纯数字项作为延迟毫秒数 */
function extractDelay(modifiers: Record<string, boolean>): number {
  for (const key of Object.keys(modifiers)) {
    const n = Number(key);
    if (Number.isInteger(n) && n > 0) return n;
  }
  return DEFAULT_DELAY;
}

export const vDebounce: Directive<HTMLElement> = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    const state: DebounceState = {
      eventName: typeof binding.arg === "string" && binding.arg ? binding.arg : "click",
      delay: extractDelay(binding.modifiers),
      latest: (...args: unknown[]) => {
        if (typeof binding.value === "function") binding.value(...args);
      },
      listener: () => {
        // 重复触发：重置计时
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(() => {
          state.timer = null;
          state.latest();
        }, state.delay);
      },
    };

    (el as unknown as Record<symbol, DebounceState>)[STATE_KEY] = state;
    el.addEventListener(state.eventName, state.listener);
  },

  updated(el: HTMLElement, binding: DirectiveBinding) {
    const state = (el as unknown as Record<symbol, DebounceState | undefined>)[STATE_KEY];
    if (!state) return;
    // 原地替换最新回调，稳定监听器无需重绑
    if (typeof binding.value === "function") {
      state.latest = (...args: unknown[]) => binding.value(...args);
    }
  },

  unmounted(el: HTMLElement) {
    const state = (el as unknown as Record<symbol, DebounceState | undefined>)[STATE_KEY];
    if (!state) return;
    if (state.timer) clearTimeout(state.timer);
    el.removeEventListener(state.eventName, state.listener);
    delete (el as unknown as Record<symbol, unknown>)[STATE_KEY];
  },
};

export default vDebounce;
