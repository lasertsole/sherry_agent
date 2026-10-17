/**
 * File mentions in the composer (integration).
 *
 * Contract: a workspace file dropped on the input box (or handed to it by the
 * file tree's "Add to chat") lands as an atomic chip; what gets SENT is the
 * `@relative/path` the chip carries, never its visible label — and the draft
 * model tracks the same text.
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import inputBox from '@/components/chat/inputBox.vue';
import { FILE_MENTION_CLASS, FILE_MENTION_MIME } from '@/composables/use-file-mention';

function mountInput(props: Record<string, unknown> = {}) {
  return mount(inputBox, {
    props,
    global: { stubs: { Button: { template: '<button class="send-stub">send</button>' } } }
  });
}

/**
 * A DataTransfer stand-in: happy-dom's is not constructible with data.
 * @param data
 */
function fakeDataTransfer(data: Record<string, string>): DataTransfer {
  return {
    types: Object.keys(data),
    dropEffect: 'none',
    effectAllowed: 'all',
    getData: (type: string) => data[type] ?? '',
    setData: () => undefined
  } as unknown as DataTransfer;
}

/**
 * Every `update:draft` a wrapper emitted (the controlled draft's value trail).
 * @param wrapper
 */
function draftUpdates(wrapper: ReturnType<typeof mountInput>): unknown[] {
  return (wrapper.emitted('update:draft') ?? []).map(args => args[0]);
}

/**
 * Dispatch a drag event carrying `payload` for the mention MIME.
 * @param el
 * @param type
 * @param data
 */
function dragEvent(el: HTMLElement, type: string, data: Record<string, string>): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: fakeDataTransfer(data) });
  el.dispatchEvent(event);
  return event;
}

describe('inputBox file mentions', () => {
  it('turns a dropped workspace file into a chip and sends its @path', async () => {
    const wrapper = mountInput();
    const root = wrapper.find('.root').element as HTMLElement;
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    const payload = { type: 'file', path: 'src/foo.py', name: 'foo.py' };

    dragEvent(root, 'drop', { [FILE_MENTION_MIME]: JSON.stringify(payload) });
    await wrapper.vm.$nextTick();

    const chip = inputEl.querySelector(`.${FILE_MENTION_CLASS}`) as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.dataset.filePath).toBe('@./src/foo.py');
    // The visible label is the name, never the mention text…
    expect(chip.textContent).toContain('foo.py');
    // …while the DRAFT (what the input box validates/sends) is the mention text.
    expect(draftUpdates(wrapper).at(-1)).toBe('@./src/foo.py');

    inputEl.appendChild(document.createTextNode('please review'));
    inputEl.dispatchEvent(new InputEvent('input', { bubbles: true }));
    await wrapper.vm.$nextTick();

    // Enter sends immediately (the send BUTTON is debounced by v-debounce).
    await wrapper.find('.inputBox').trigger('keydown.enter');
    expect(wrapper.emitted('send')?.[0]?.[0]).toBe('@./src/foo.py please review');
  });

  it('marks the row type on the chip for a directory', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    (wrapper.vm as unknown as { addFileMention: (p: unknown) => void }).addFileMention({
      type: 'dir',
      path: 'src',
      name: 'src'
    });
    await wrapper.vm.$nextTick();

    const chip = inputEl.querySelector(`.${FILE_MENTION_CLASS}`) as HTMLElement;
    expect(chip.dataset.fileType).toBe('dir');
    expect(chip.dataset.filePath).toBe('@./src/');
  });

  it('ignores a drop that carries no mention payload', async () => {
    const wrapper = mountInput();
    const root = wrapper.find('.root').element as HTMLElement;
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;

    dragEvent(root, 'drop', { 'text/plain': 'hello' });
    await wrapper.vm.$nextTick();

    expect(inputEl.querySelector(`.${FILE_MENTION_CLASS}`)).toBeNull();
    expect(draftUpdates(wrapper)).toEqual([]);
  });

  it('refuses a programmatic mention while input is disabled', async () => {
    const wrapper = mountInput({ disabled: true });
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;

    (wrapper.vm as unknown as { addFileMention: (p: unknown) => void }).addFileMention({
      type: 'file',
      path: 'a.py',
      name: 'a.py'
    });
    await wrapper.vm.$nextTick();

    expect(inputEl.querySelector(`.${FILE_MENTION_CLASS}`)).toBeNull();
  });

  it('highlights the composer only for a workspace-file drag', async () => {
    const wrapper = mountInput();
    const root = wrapper.find('.root').element as HTMLElement;

    dragEvent(root, 'dragover', { 'text/plain': 'foreign' });
    await wrapper.vm.$nextTick();
    expect(root.classList.contains('drag-over')).toBe(false);

    dragEvent(root, 'dragover', { [FILE_MENTION_MIME]: '{}' });
    await wrapper.vm.$nextTick();
    expect(root.classList.contains('drag-over')).toBe(true);

    dragEvent(root, 'dragleave', {});
    await wrapper.vm.$nextTick();
    expect(root.classList.contains('drag-over')).toBe(false);
  });

  it('clears the input (chips included) after a send', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    (wrapper.vm as unknown as { addFileMention: (p: unknown) => void }).addFileMention({
      type: 'file',
      path: 'a.py',
      name: 'a.py'
    });
    await wrapper.vm.$nextTick();

    await wrapper.find('.inputBox').trigger('keydown.enter');

    expect(wrapper.emitted('send')?.[0]?.[0]).toBe('@./a.py');
    expect(inputEl.querySelector(`.${FILE_MENTION_CLASS}`)).toBeNull();
    expect(draftUpdates(wrapper).at(-1)).toBe('');
  });
});
