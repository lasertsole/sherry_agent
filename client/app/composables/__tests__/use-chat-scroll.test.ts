import { describe, it, expect, beforeEach } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { mount, type VueWrapper } from '@vue/test-utils';
import { useChatScroll } from '../use-chat-scroll';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';

type ScrollVm = {
  // `wrapper.vm` unwraps refs, so the template ref is exposed as the element itself.
  scrollContainerRef: HTMLElement | null;
  showScrollBottom: boolean;
  scrollToBottom: () => void;
  updateScrollBottomBtn: () => void;
};

const Harness = defineComponent({
  props: { messages: { type: Array as () => MessageItem[], default: () => [] } },
  setup(props) {
    return { ...useChatScroll(() => props.messages) };
  },
  template: `<div ref="scrollContainerRef" @scroll="updateScrollBottomBtn"></div>`
});

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'x',
  id: 1,
  turn_num: 0,
  timestamp: 't',
  ...over
});

/**
 * Force deterministic geometry on the happy-dom element.
 * @param el
 * @param scrollHeight
 * @param clientHeight
 * @param scrollTop
 */
const setGeometry = (el: HTMLElement, scrollHeight: number, clientHeight: number, scrollTop: number) => {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
  el.scrollTop = scrollTop;
};

describe('useChatScroll', () => {
  let wrapper: VueWrapper;
  let vm: ScrollVm;

  beforeEach(() => {
    wrapper = mount(Harness, { props: { messages: [] } });
    vm = wrapper.vm as unknown as ScrollVm;
  });

  it('binds the scroll container template ref and starts with the button hidden', () => {
    expect(vm.scrollContainerRef).toBeInstanceOf(HTMLElement);
    expect(vm.showScrollBottom).toBe(false);
  });

  it('shows the button only when the distance to the bottom exceeds the 80px threshold', async () => {
    const el = vm.scrollContainerRef!;
    setGeometry(el, 1000, 100, 0); // 900px away
    await wrapper.trigger('scroll');
    expect(vm.showScrollBottom).toBe(true);

    setGeometry(el, 1000, 100, 950); // 50px away
    await wrapper.trigger('scroll');
    expect(vm.showScrollBottom).toBe(false);
  });

  it('scrollToBottom jumps to the bottom after the DOM update and hides the button', async () => {
    const el = vm.scrollContainerRef!;
    setGeometry(el, 500, 100, 0);
    vm.scrollToBottom();
    await nextTick();
    expect(el.scrollTop).toBe(500);
    expect(vm.showScrollBottom).toBe(false);
  });

  it('always follows a newly appended USER message even when scrolled far up', async () => {
    const el = vm.scrollContainerRef!;
    setGeometry(el, 500, 100, 0);
    await wrapper.setProps({ messages: [msg({ id: 1, role: CHAT_ROLE.USER })] });
    await nextTick();
    expect(el.scrollTop).toBe(500);
  });
});
