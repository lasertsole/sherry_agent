import type { Directive, DirectiveBinding } from 'vue';

/**
 * v-debounce — event debounce directive.
 *
 * Usage:
 *   v-debounce:click.500="handleSend"
 *     - arg (`:click`): the DOM event name to listen to, defaults to `click`
 *     - modifiers (`.500`): debounce delay in milliseconds; the first purely numeric
 *       modifier wins, default 300ms
 *   The value is the callback to debounce; after the event fires, the callback runs
 *   only after a silent `delay` ms, and repeated triggers during that window restart
 *   the timer.
 *
 * Implementation notes:
 *   - The event listener is bound only once (mounted) and forwards to the "latest
 *     callback" through a stable wrapper function, so updated needs no
 *     unbind/rebind (inline arrow functions produce a fresh reference on every
 *     render).
 *   - On unmounted, any pending timer is cleared and the listener is unbound so no
 *     callback fires after the component is unmounted.
 */

/** Default debounce delay in milliseconds when none is specified explicitly */
const DEFAULT_DELAY = 300;

/** Internal state attached to the element (used for unbinding and cleanup) */
interface DebounceState {
  eventName: string;
  delay: number;
  /** The latest user-bound callback (replaced in place on updated) */
  latest: (...args: unknown[]) => void;
  /** Stable event-listener wrapper (created on mounted, unbound on unmounted) */
  listener: (event: Event) => void;
  /** Handle of the currently pending timer */
  timer: ReturnType<typeof setTimeout> | null;
}

/** Property key used to store the state on the element (a Symbol avoids clashes with business properties) */
const STATE_KEY = Symbol('v-debounce-state');

/** Extract the first purely numeric modifier as the delay in milliseconds */
function extractDelay(modifiers: Partial<Record<string, boolean>>): number {
  for (const key of Object.keys(modifiers)) {
    const n = Number(key);
    if (Number.isInteger(n) && n > 0) return n;
  }
  return DEFAULT_DELAY;
}

export const vDebounce: Directive<HTMLElement> = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    const state: DebounceState = {
      eventName: typeof binding.arg === 'string' && binding.arg ? binding.arg : 'click',
      delay: extractDelay(binding.modifiers),
      latest: (...args: unknown[]) => {
        if (typeof binding.value === 'function') binding.value(...args);
      },
      listener: () => {
        // Repeated trigger: reset the timer
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(() => {
          state.timer = null;
          state.latest();
        }, state.delay);
      },
      timer: null
    };

    (el as unknown as Record<symbol, DebounceState>)[STATE_KEY] = state;
    el.addEventListener(state.eventName, state.listener);
  },

  updated(el: HTMLElement, binding: DirectiveBinding) {
    const state = (el as unknown as Record<symbol, DebounceState | undefined>)[STATE_KEY];
    if (!state) return;
    // Replace the latest callback in place; the stable listener needs no rebinding
    if (typeof binding.value === 'function') {
      state.latest = (...args: unknown[]) => binding.value(...args);
    }
  },

  unmounted(el: HTMLElement) {
    const state = (el as unknown as Record<symbol, DebounceState | undefined>)[STATE_KEY];
    if (!state) return;
    if (state.timer) clearTimeout(state.timer);
    el.removeEventListener(state.eventName, state.listener);
    delete (el as unknown as Record<symbol, unknown>)[STATE_KEY];
  }
};

export default vDebounce;
