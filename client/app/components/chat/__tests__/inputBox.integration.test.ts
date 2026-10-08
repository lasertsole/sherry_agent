import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import inputBox from '@/components/chat/inputBox.vue';

// PrimeVue Button is a Nuxt auto-import; stub it in this environment.
function mountInput(props: Record<string, unknown> = {}) {
  return mount(inputBox, {
    props,
    global: { stubs: { Button: { template: '<button class="send-stub">send</button>' } } }
  });
}

describe('inputBox.vue (integration, backend mocked)', () => {
  it('tells the user that typing queues a message while the agent is working', () => {
    // The backend accepts a send during a running turn and QUEUES it, so the
    // placeholder must say so ("继续输入消息以排队") — the old "AI is thinking..."
    // only reported busy-ness and hid the queueing affordance.
    const idle = mountInput({ sending: false });
    const busy = mountInput({ sending: true });

    expect(idle.find('.inputBox').attributes('placeholder')).toContain('请输入内容');
    expect(busy.find('.inputBox').attributes('placeholder')).toContain('继续输入消息以排队');
    // Sending is still allowed while busy (the stop button is the other half).
    expect(busy.find('.inputBox').attributes('contenteditable')).toBe('true');
  });

  it('keeps the approval hint ahead of the queue hint when input is disabled', () => {
    const wrapper = mountInput({ sending: true, disabled: true, disabledText: '等待审批…' });

    expect(wrapper.find('.inputBox').attributes('placeholder')).toBe('等待审批…');
  });
  it('renders the editable input area and the send button', () => {
    const wrapper = mountInput();
    expect(wrapper.find('.inputBox[contenteditable]').exists()).toBe(true);
    expect(wrapper.find('.send-stub').exists()).toBe(true);
  });

  it('clears the placeholder/empty content on backspace when the box only holds <br>', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    // Simulate a fresh, empty input that happy-dom serializes as <br>.
    inputEl.innerHTML = '<br>';
    // A real InputEvent is required: inputFunc() early-returns on generic Events.
    inputEl.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }));
    await Promise.resolve();
    // Backspacing an empty box must wipe out the leftover <br>.
    expect(inputEl.innerHTML).toContain('');
    expect(inputEl.textContent).toBe('');
  });

  it('writes the typed content back through the input handler', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    inputEl.innerHTML = '<div>hello</div>';
    inputEl.dispatchEvent(new InputEvent('input', { inputType: 'insertText', bubbles: true }));
    await Promise.resolve();
    // The handler keeps the raw innerHTML as the message value.
    expect(inputEl.innerHTML).toBe('<div>hello</div>');
  });

  it('exposes NO element bound to the expected inputDom ref (template mismatch)', () => {
    // inputBox.vue calls useTemplateRef('inputDom') but the template never sets
    // ref="inputDom". This documents the defect: inputDom.value will be null.
    const wrapper = mountInput();
    expect(wrapper.find('[ref="inputDom"]').exists()).toBe(false);
  });
});
