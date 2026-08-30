// src/directives/debounce.ts
import { DirectiveBinding, ObjectDirective, VNode } from "vue";

// ============ 类型定义 ============

interface DebounceBinding {
  /** 需要防抖的回调函数 */
  handler: (...args: unknown[]) => void;
  /** 延迟毫秒数，默认 500ms */
  delay?: number;
  /** 是否立即执行（首次触发立即执行，后续防抖），默认 false（先等待后执行） */
  immediate?: boolean;
}

interface IDebounceOptions {
  delay: number;
  immediate: boolean;
}

/** 扩展 HTMLElement，存储防抖相关状态 */
interface DebounceElement extends HTMLElement {
  _debounce?: {
    originalHandler: (...args: unknown[]) => void;
    debouncedHandler: (event: Event) => void;
    eventType: string;
    timeout: ReturnType<typeof setTimeout> | null;
  } & IDebounceOptions;
}

// ============ 核心逻辑 ============

/**
 * 创建防抖函数并绑定事件监听
 */
function createDebounce(
  el: DebounceElement,
  eventType: string,
  handler: (...args: unknown[]) => void,
  opts: IDebounceOptions,
): void {
  let timeout: ReturnType<typeof setTimeout> | null = null;

  const debouncedHandler = (event: Event) => {
    if (opts.immediate) {
      // 立即执行模式：首次立即调用，后续防抖
      if (timeout) {
        clearTimeout(timeout);
      } else {
        handler(event);
      }
    } else if (timeout) {
      // 延迟执行模式：每次触发都清除并重新计时
      clearTimeout(timeout);
    }

    timeout = setTimeout(() => {
      if (!opts.immediate) {
        handler(event);
      }
      timeout = null;
    }, opts.delay);
  };

  // 将防抖状态存储在 DOM 元素上，供卸载时清理
  el._debounce = {
    originalHandler: handler,
    debouncedHandler,
    eventType,
    timeout,
    delay: opts.delay,
    immediate: opts.immediate,
  };

  el.addEventListener(eventType, debouncedHandler);
}

/**
 * 解析指令绑定值并创建防抖
 */
function bindDebounce(el: DebounceElement, binding: DirectiveBinding): void {
  const { value, arg, modifiers } = binding;

  // 事件类型：通过指令参数指定，如 v-debounce:input，默认 click
  const eventType = arg || "click";

  // 延迟时间：通过修饰符指定，如 v-debounce:click.300，默认 500ms
  let delay = 500;
  const timeModifiers = Object.keys(modifiers)
    .filter((key) => !isNaN(Number(key)))
    .map(Number);
  if (timeModifiers.length > 0) {
    delay = timeModifiers[0];
  }

  let handler: (...args: unknown[]) => void;
  let finalDelay = delay;
  let finalImmediate = false;

  // 支持两种绑定值写法
  if (typeof value === "function") {
    // 简写：v-debounce="handleEvent"
    handler = value;
  } else if (value && typeof value === "object" && "handler" in value) {
    // 对象配置：v-debounce="{ handler: fn, delay: 1000, immediate: true }"
    handler = value.handler;
    finalDelay = value.delay || delay;
    finalImmediate = !!value.immediate;
  } else {
    console.warn("v-debounce 需要绑定函数或包含 handler 的对象");
    return;
  }

  createDebounce(el, eventType, handler, {
    delay: finalDelay,
    immediate: finalImmediate,
  });
}

/**
 * 移除防抖事件监听并清理定时器
 */
function unbindDebounce(el: DebounceElement): void {
  if (el._debounce) {
    clearTimeout(el._debounce.timeout);
    el.removeEventListener(
      el._debounce.eventType,
      el._debounce.debouncedHandler,
    );
    delete el._debounce;
  }
}

// ============ 指令导出 ============

export const vDebounce: ObjectDirective<
  DebounceElement,
  (() => void) | DebounceBinding
> = {
  // 组件挂载时创建防抖并绑定事件
  mounted(el: DebounceElement, binding: DirectiveBinding) {
    bindDebounce(el, binding);
  },

  // 组件更新时（绑定值变化）重新绑定
  updated(el: DebounceElement, binding: DirectiveBinding) {
    unbindDebounce(el);
    bindDebounce(el, binding);
  },

  // 组件卸载前清理
  beforeUnmount(el: DebounceElement) {
    unbindDebounce(el);
  },
};