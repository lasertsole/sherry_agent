import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import inputBox from '@/components/chat/inputBox.vue';
import { fetchVoiceStatus, requestTranscription } from '~/composables/bridge/voice';

// The voice bridge is the network seam: availability follows the faked status,
// the transcript is a fixed string the round-trip test asserts on.
vi.mock('~/composables/bridge/voice', async importOriginal => {
  const actual = await importOriginal<typeof import('~/composables/bridge/voice')>();
  return {
    ...actual,
    fetchVoiceStatus: vi.fn(async () => ({ model_dir_present: true, daemon_ready: false })),
    requestTranscription: vi.fn(async () => '语音转录结果')
  };
});

/**
 * The browser recording stack happy-dom does not ship: getUserMedia returns a
 * one-track stream, MediaRecorder is a manual double whose stop() fires
 * onstop, AudioContext decodes to a fixed 2 s stereo buffer.
 * @returns The fake stream's track spy (stop assertions).
 */
function installRecordingStack() {
  const track = { stop: vi.fn() };
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [track] })) },
    configurable: true
  });
  class FakeMediaRecorder {
    static isTypeSupported = vi.fn((mime: string) => mime === 'audio/webm;codecs=opus');
    mimeType = 'audio/webm;codecs=opus';
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    start(): void {
      // Simulate the recorder's 1 s timeslice: one chunk of payload arrives.
      queueMicrotask(() =>
        this.ondataavailable?.({ data: new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/webm' }) })
      );
    }
    stop(): void {
      this.onstop?.();
    }
  }
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  class FakeAudioContext {
    async decodeAudioData() {
      return {
        duration: 2.0,
        numberOfChannels: 2,
        length: 48000,
        sampleRate: 48000,
        getChannelData: (ch: number) => new Float32Array(48000).fill(ch === 0 ? 0.5 : -0.5)
      };
    }
    async close() {}
  }
  vi.stubGlobal(
    'AudioContext',
    vi.fn(function fakeAudioContextCtor() {
      return new FakeAudioContext();
    })
  );
  return { track };
}

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

describe('inputBox.vue voice input (integration, bridge mocked)', () => {
  beforeEach(() => {
    // mockReset (not mockClear): a previous test's mockResolvedValue(false)
    // must not leak its implementation into the next mount.
    vi.mocked(fetchVoiceStatus).mockReset().mockResolvedValue({
      model_dir_present: true,
      daemon_ready: false
    });
    vi.mocked(requestTranscription).mockReset().mockResolvedValue('语音转录结果');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('renders the mic button next to send and hides it while the box is disabled', async () => {
    const open = mountInput();
    await Promise.resolve();
    expect(open.findAll('.sendActions button').length).toBe(2); // mic + send
    expect(open.find('.voiceTimer').exists()).toBe(false);

    const locked = mountInput({ disabled: true, disabledText: '等待审批…' });
    await Promise.resolve();
    expect(locked.findAll('.sendActions button').length).toBe(1); // send only
  });

  it('keeps the unconfigured mic clickable and explains on hover AND on click', async () => {
    // A disabled button cannot speak: the click itself toasts the reason, the
    // title stays as the hover hint, and no recording may start.
    vi.mocked(fetchVoiceStatus).mockResolvedValue({ model_dir_present: false, daemon_ready: false });
    const wrapper = mountInput();
    await Promise.resolve();
    await Promise.resolve();

    const mic = wrapper.find('.sendActions button');
    expect(mic.attributes('title')).toContain('未配置');
    expect(mic.attributes('disabled')).toBeUndefined();

    await mic.trigger('click');
    await Promise.resolve();
    await Promise.resolve();
    // No recording started and the badge never appears — the click only
    // produced the (test-unregistered) toast.
    expect(wrapper.find('.voice-recording').exists()).toBe(false);
  });

  it('runs a full round: click → recording UI → stop → transcript lands in the box', async () => {
    vi.useFakeTimers();
    const { track } = installRecordingStack();
    const wrapper = mountInput();
    await vi.advanceTimersByTimeAsync(0);
    const mic = wrapper.find('.sendActions button');

    await mic.trigger('click');
    await vi.advanceTimersByTimeAsync(300);
    expect(wrapper.find('.voice-recording').exists()).toBe(true);
    expect(wrapper.find('.voiceTimer').exists()).toBe(true);

    await mic.trigger('click');
    await vi.advanceTimersByTimeAsync(300);

    const box = wrapper.find('.inputBox').element as HTMLElement;
    expect(box.textContent).toContain('语音转录结果');
    expect(track.stop).toHaveBeenCalled();
    const drafts = wrapper.emitted('update:draft');
    expect(String(drafts?.at(-1)?.[0])).toContain('语音转录结果');
    expect(vi.mocked(requestTranscription)).toHaveBeenCalledTimes(1);
  });

  it('inserts voice text at the caret and syncs the draft model', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    inputEl.innerHTML = '<span>existing</span>';
    const selection = window.getSelection()!;
    const range = document.createRange();
    range.selectNodeContents(inputEl);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);

    (wrapper.vm as unknown as { insertVoiceText: (text: string) => void }).insertVoiceText('语音结果');
    await Promise.resolve();

    expect(inputEl.textContent).toContain('语音结果');
    expect(inputEl.textContent).toContain('existing');
    const drafts = wrapper.emitted('update:draft');
    expect(String(drafts?.at(-1)?.[0])).toContain('语音结果');
  });

  it('appends voice text at the end when the caret is elsewhere', async () => {
    const wrapper = mountInput();
    const inputEl = wrapper.find('.inputBox').element as HTMLElement;
    inputEl.innerHTML = '<span>existing</span>';
    window.getSelection()?.removeAllRanges();

    (wrapper.vm as unknown as { insertVoiceText: (text: string) => void }).insertVoiceText('追加');
    await Promise.resolve();

    expect(inputEl.textContent).toContain('existing追加');
  });
});
