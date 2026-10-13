import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { defineComponent } from 'vue';
import { mount, type VueWrapper } from '@vue/test-utils';
import { useMessageCopy, fallbackCopyText } from '../use-message-copy';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';

type CopyVm = {
  copiedMessageId: number | null;
  canCopyMessage: (m: MessageItem) => boolean;
  copyMessage: (m: MessageItem) => Promise<void>;
};

const Harness = defineComponent({
  setup() {
    return { ...useMessageCopy() };
  },
  template: '<div/>'
});

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'hello',
  id: 7,
  turn_num: 0,
  timestamp: 't',
  ...over
});

const setClipboard = (writeText: (() => Promise<void>) | undefined) => {
  Object.defineProperty(navigator, 'clipboard', { value: writeText ? { writeText } : undefined, configurable: true });
  Object.defineProperty(window, 'isSecureContext', { value: !!writeText, configurable: true });
};

describe('useMessageCopy', () => {
  let wrapper: VueWrapper;
  let vm: CopyVm;

  beforeEach(() => {
    (document as unknown as { execCommand: unknown }).execCommand = vi.fn(() => false);
    setClipboard(undefined);
    wrapper = mount(Harness);
    vm = wrapper.vm as unknown as CopyVm;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('canCopyMessage allows user/AI messages with a non-empty body only', () => {
    expect(vm.canCopyMessage(msg({ role: CHAT_ROLE.USER, content: 'hi' }))).toBe(true);
    expect(vm.canCopyMessage(msg({ role: CHAT_ROLE.AI, content: 'hi' }))).toBe(true);
    expect(vm.canCopyMessage(msg({ role: CHAT_ROLE.TOOL, content: 'hi' }))).toBe(false);
    expect(vm.canCopyMessage(msg({ role: CHAT_ROLE.USER, content: '' }))).toBe(false);
    expect(vm.canCopyMessage(msg({ role: CHAT_ROLE.AI, content: '   ' }))).toBe(false);
  });

  it('shows the copied id on success and clears it after 1500ms', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard(writeText);
    await vm.copyMessage(msg({ id: 42 }));
    expect(writeText).toHaveBeenCalledWith('hello');
    expect(vm.copiedMessageId).toBe(42);
    vi.advanceTimersByTime(1500);
    expect(vm.copiedMessageId).toBeNull();
  });

  it('keeps no feedback and does not throw when both copy paths fail', async () => {
    await expect(vm.copyMessage(msg({ id: 9 }))).resolves.toBeUndefined();
    expect(vm.copiedMessageId).toBeNull();
  });

  it('fallbackCopyText appends and removes a hidden textarea, returning execCommand result', () => {
    const exec = vi.fn(() => true);
    (document as unknown as { execCommand: unknown }).execCommand = exec;
    const before = document.body.children.length;
    expect(fallbackCopyText('abc')).toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
    expect(document.body.children.length).toBe(before);
  });
});
