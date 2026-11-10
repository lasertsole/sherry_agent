/**
 * `v-debounce` — the shared request-button guard.
 *
 * Two properties the plan relies on when the directive is sprinkled over every
 * request button: the callback fires once after the quiet window (rapid clicks
 * restart it), and an unmount with a pending timer cancels it (no orphaned
 * request after a right-sidebar tab closes).
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { defineComponent, h, withDirectives } from 'vue';
import { mount } from '@vue/test-utils';
import { vDebounce } from '../debounce';

/** A button carrying `v-debounce:click.500="fire"`. */
const DebouncedButton = defineComponent({
  props: { fire: { type: Function, required: true } },
  setup(props) {
    return () => withDirectives(h('button', 'go'), [[vDebounce, props.fire, 'click', { 500: true }]]);
  }
});

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('v-debounce', () => {
  it('fires once after the quiet window and restarts it on a repeat click', async () => {
    const fire = vi.fn();
    const wrapper = mount(DebouncedButton, { props: { fire } });

    await wrapper.get('button').trigger('click');
    vi.advanceTimersByTime(400);
    await wrapper.get('button').trigger('click');
    vi.advanceTimersByTime(400);
    expect(fire).not.toHaveBeenCalled();

    vi.advanceTimersByTime(100);
    expect(fire).toHaveBeenCalledTimes(1);
  });

  it('cancels a pending callback when the element unmounts', async () => {
    const fire = vi.fn();
    const wrapper = mount(DebouncedButton, { props: { fire } });

    await wrapper.get('button').trigger('click');
    wrapper.unmount();
    vi.advanceTimersByTime(1000);

    expect(fire).not.toHaveBeenCalled();
  });
});
