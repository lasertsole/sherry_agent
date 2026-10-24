import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { effectScope } from 'vue';
import {
  MAX_RECORDING_SEC,
  MIN_AUDIO_SEC,
  TARGET_SAMPLE_RATE,
  encodeWavPcm16,
  mixdownToMono,
  resampleLinear,
  useVoiceInput
} from '../use-voice-input';
import { TranscribeRejected } from '../bridge/voice';

/**
 * The voice-input state machine against a faked recording stack: the bridge
 * (`fetchVoiceStatus` / `requestTranscription`) is mocked at its module seam
 * (the real `TranscribeRejected` class stays for instanceof), and
 * getUserMedia / MediaRecorder / AudioContext are stubbed as globals —
 * happy-dom ships none of them.
 */

const { fetchVoiceStatusMock, requestTranscriptionMock } = vi.hoisted(() => ({
  fetchVoiceStatusMock: vi.fn<() => Promise<{ model_dir_present: boolean; daemon_ready: boolean } | null>>(),
  requestTranscriptionMock: vi.fn<() => Promise<string>>()
}));

vi.mock('../bridge/voice', async importOriginal => {
  const actual = await importOriginal<typeof import('../bridge/voice')>();
  return {
    ...actual,
    fetchVoiceStatus: fetchVoiceStatusMock,
    requestTranscription: requestTranscriptionMock
  };
});

/** A MediaRecorder double whose stop() fires the composable's onstop resolver. */
class FakeMediaRecorder {
  static readonly instances: FakeMediaRecorder[] = [];
  static isTypeSupported = vi.fn((mime: string) => mime === 'audio/webm;codecs=opus');

  mimeType = 'audio/webm;codecs=opus';
  state = 'inactive';
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  started = 0;
  stopped = 0;

  constructor(
    public stream: unknown,
    public options?: { mimeType?: string }
  ) {
    FakeMediaRecorder.instances.push(this);
  }

  start(): void {
    this.started++;
    this.state = 'recording';
  }

  stop(): void {
    this.stopped++;
    this.state = 'inactive';
    this.onstop?.();
  }

  /**
   * Test helper: emit one timeslice.
   * @param chunk
   */
  emit(chunk: Blob): void {
    this.ondataavailable?.({ data: chunk });
  }
}

function fakeTrack() {
  return { stop: vi.fn() };
}

function installRecordingStack(options: { getUserMedia?: () => Promise<unknown>; decodeDuration?: number } = {}) {
  const track = fakeTrack();
  const getUserMedia = options.getUserMedia ?? vi.fn(() => Promise.resolve({ getTracks: () => [track] }));
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true
  });
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  // `new AudioContext()` — vitest rejects arrow-function implementations for
  // constructor calls, so the mock wraps a real class in a `function` impl.
  class FakeAudioContext {
    readonly sampleRate = 48000;
    async decodeAudioData() {
      return {
        duration: options.decodeDuration ?? 2.0,
        numberOfChannels: 2,
        length: 48000,
        sampleRate: 48000,
        getChannelData: (ch: number) => new Float32Array(48000).fill(ch === 0 ? 0.5 : -0.5)
      };
    }
    async close() {
      return undefined;
    }
  }
  vi.stubGlobal(
    'AudioContext',
    vi.fn(function fakeAudioContextCtor() {
      return new FakeAudioContext();
    })
  );
  return { track, getUserMedia };
}

/**
 * Mount one machine inside an effect scope (onScopeDispose needs one).
 * @returns The machine refs/actions plus its owning scope.
 */
function mountMachine() {
  const scope = effectScope();
  const machine = scope.run(() => useVoiceInput())!;
  return { scope, ...machine };
}

async function recordAndStop(machine: ReturnType<typeof mountMachine>, blob?: Blob) {
  await machine.startRecording();
  const instance = FakeMediaRecorder.instances.at(-1)!;
  if (blob) instance.emit(blob);
  return machine.stopRecording();
}

describe('encodeWavPcm16', () => {
  it('writes a RIFF/WAVE/fmt/data header with mono PCM16 framing', () => {
    const wav = encodeWavPcm16(new Float32Array([0, 0]), 16000);
    const text = (start: number, length: number) =>
      String.fromCharCode(...Array.from(wav.slice(start, start + length)));

    expect(text(0, 4)).toBe('RIFF');
    expect(text(8, 4)).toBe('WAVE');
    expect(text(12, 4)).toBe('fmt ');
    expect(wav[20]).toBe(1); // PCM
    expect(wav[22]).toBe(1); // mono
    expect(new DataView(wav.buffer).getUint32(24, true)).toBe(16000);
    expect(new DataView(wav.buffer).getUint32(28, true)).toBe(32000); // byte rate
    expect(text(36, 4)).toBe('data');
    expect(new DataView(wav.buffer).getUint32(40, true)).toBe(4); // 2 samples × 2 bytes
    expect(wav.length).toBe(44 + 4);
  });

  it('quantizes and clamps samples', () => {
    const wav = encodeWavPcm16(new Float32Array([0.5, -1, 1.5]), 16000);
    const view = new DataView(wav.buffer);
    expect(view.getInt16(44, true)).toBe(Math.trunc(0.5 * 0x7fff));
    expect(view.getInt16(46, true)).toBe(-0x8000);
    expect(view.getInt16(48, true)).toBe(0x7fff); // 1.5 clamps to 1
  });
});

describe('resampleLinear / mixdownToMono', () => {
  it('shortens by the rate ratio and interpolates midpoints', () => {
    // ratio 2.5 (40k → 16k): output positions 0 and 2.5 → samples[0], ½·[2]+½·[3]
    const samples = new Float32Array([0, 0.25, 0.5, 0.75, 1]);
    const out = resampleLinear(samples, 40000, TARGET_SAMPLE_RATE);
    expect(out.length).toBe(2);
    expect(out[0]).toBeCloseTo(0);
    expect(out[1]).toBeCloseTo(0.625);
  });

  it('is a no-op when the rates already match', () => {
    const samples = new Float32Array([0.1, 0.2]);
    expect(resampleLinear(samples, 16000, 16000)).toBe(samples);
  });

  it('averages channels in the mixdown', () => {
    const buffer = {
      numberOfChannels: 2,
      length: 2,
      getChannelData: (ch: number) => new Float32Array([ch === 0 ? 1 : 0, ch === 0 ? 0.5 : 0.5])
    } as unknown as AudioBuffer;
    const mono = mixdownToMono(buffer);
    expect(Array.from(mono)).toEqual([0.5, 0.5]);
  });
});

describe('useVoiceInput', () => {
  beforeEach(() => {
    FakeMediaRecorder.instances.length = 0;
    FakeMediaRecorder.isTypeSupported = vi.fn((mime: string) => mime === 'audio/webm;codecs=opus');
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('probes availability: only the weights gate it, a cold daemon does not', async () => {
    fetchVoiceStatusMock.mockResolvedValue({ model_dir_present: true, daemon_ready: false });
    const voice = mountMachine();
    await voice.probeAvailability();
    expect(voice.isSttAvailable.value).toBe(true);

    fetchVoiceStatusMock.mockResolvedValue({ model_dir_present: false, daemon_ready: false });
    await voice.probeAvailability();
    expect(voice.isSttAvailable.value).toBe(false);

    fetchVoiceStatusMock.mockResolvedValue(null);
    await voice.probeAvailability();
    expect(voice.isSttAvailable.value).toBeNull();
  });

  it('starts recording with a supported mime and stops into transcribing → idle', async () => {
    const { track, getUserMedia } = installRecordingStack();
    requestTranscriptionMock.mockResolvedValue('你好');
    const voice = mountMachine();

    const started = await voice.startRecording();
    expect(started).toBe(true);
    expect(voice.state.value).toBe('recording');
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
    });
    const instance = FakeMediaRecorder.instances.at(-1)!;
    expect(instance.options?.mimeType).toBe('audio/webm;codecs=opus');
    expect(instance.started).toBe(1);

    instance.emit(new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/webm' }));
    const result = await voice.stopRecording();

    expect(result).toEqual({ ok: true, text: '你好' });
    expect(voice.state.value).toBe('idle');
    expect(instance.stopped).toBe(1);
    expect(track.stop).toHaveBeenCalled();
    expect(requestTranscriptionMock).toHaveBeenCalledTimes(1);
  });

  it('maps a denied getUserMedia to a permission error without touching state', async () => {
    const { getUserMedia } = installRecordingStack({
      getUserMedia: vi.fn(() => Promise.reject(new DOMException('denied', 'NotAllowedError')))
    });
    const voice = mountMachine();

    const started = await voice.startRecording();

    expect(started).toBe(false);
    expect(voice.state.value).toBe('idle');
    expect(voice.lastError.value).toBe('permission');
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  it('reports unsupported when the recording stack is missing', async () => {
    Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true });
    const voice = mountMachine();

    const started = await voice.startRecording();

    expect(started).toBe(false);
    expect(voice.lastError.value).toBe('unsupported');
  });

  it('refuses to start while unavailable and while not idle', async () => {
    installRecordingStack();
    const voice = mountMachine();
    voice.isSttAvailable.value = false;
    expect(await voice.startRecording()).toBe(false);

    voice.isSttAvailable.value = true;
    expect(await voice.startRecording()).toBe(true);
    expect(await voice.startRecording()).toBe(false); // already recording
  });

  it('rejects a too-short recording before any request', async () => {
    installRecordingStack({ decodeDuration: MIN_AUDIO_SEC / 2 });
    requestTranscriptionMock.mockResolvedValue('不应被调用');
    const voice = mountMachine();

    const result = await recordAndStop(voice, new Blob([new Uint8Array([1])], { type: 'audio/webm' }));

    expect(result).toEqual({ ok: false, reason: 'too_short' });
    expect(requestTranscriptionMock).not.toHaveBeenCalled();
    expect(voice.state.value).toBe('idle');
  });

  it('surfaces a failed transcription and keeps the error kind', async () => {
    installRecordingStack();
    requestTranscriptionMock.mockRejectedValue(new TranscribeRejected('error', 'boom'));
    const voice = mountMachine();

    const result = await recordAndStop(voice, new Blob([new Uint8Array([1])], { type: 'audio/webm' }));

    expect(result.ok).toBe(false);
    expect(result.reason).toBe('failed');
    expect(voice.lastError.value).toBe('failed');
    expect(voice.state.value).toBe('idle');
  });

  it('marks unavailable from the typed rejection', async () => {
    installRecordingStack();
    requestTranscriptionMock.mockRejectedValue(new TranscribeRejected('unavailable', 'no weights'));
    const voice = mountMachine();

    const result = await recordAndStop(voice, new Blob([new Uint8Array([1])], { type: 'audio/webm' }));

    expect(result.reason).toBe('unavailable');
  });

  it('retries through the warming window and succeeds', async () => {
    installRecordingStack();
    requestTranscriptionMock
      .mockRejectedValueOnce(new TranscribeRejected('warming', 'cold'))
      .mockRejectedValueOnce(new TranscribeRejected('warming', 'still cold'))
      .mockResolvedValue('终于好了');
    const voice = mountMachine();

    const pending = recordAndStop(voice, new Blob([new Uint8Array([1])], { type: 'audio/webm' }));
    await vi.advanceTimersByTimeAsync(2 * 5000 + 10);
    const result = await pending;

    expect(result).toEqual({ ok: true, text: '终于好了' });
    expect(requestTranscriptionMock).toHaveBeenCalledTimes(3);
  });

  it(`gives up after the warming window and reports the failure`, async () => {
    installRecordingStack();
    requestTranscriptionMock.mockRejectedValue(new TranscribeRejected('warming', 'never ready'));
    const voice = mountMachine();

    const pending = recordAndStop(voice, new Blob([new Uint8Array([1])], { type: 'audio/webm' }));
    await vi.advanceTimersByTimeAsync((MAX_RECORDING_SEC + 10) * 1000);
    const result = await pending;

    expect(result.ok).toBe(false);
    expect(requestTranscriptionMock).toHaveBeenCalledTimes(12);
  }, 20000);

  it('auto-stops at the recording cap', async () => {
    installRecordingStack();
    const voice = mountMachine();
    await voice.startRecording();
    const instance = FakeMediaRecorder.instances.at(-1)!;

    await vi.advanceTimersByTimeAsync((MAX_RECORDING_SEC + 1) * 1000);

    expect(instance.stopped).toBe(1);
    expect(voice.state.value).not.toBe('recording');
  });
});
