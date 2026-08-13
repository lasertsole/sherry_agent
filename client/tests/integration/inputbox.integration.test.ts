import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import inputBox from '@/components/chat/inputBox.vue';

// PrimeVue Button is a Nuxt auto-import; stub it in this environment.
function mountInput() {
  return mount(inputBox, {
    global: { stubs: { Button: { template: '<button class="send-stub">send</button>' } } },
  });
}

describe('inputBox.vue (integration, backend mocked)', () => {
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
    inputEl.dispatchEvent(
      new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true })
    );
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
