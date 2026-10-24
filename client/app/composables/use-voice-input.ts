/**
 * Voice input: MediaRecorder → decodeAudioData → 16 kHz mono PCM16 WAV →
 * gateway transcription, as a UI-free state machine.
 *
 * Why the WAV detour instead of shipping MediaRecorder's own encoding
 * (usually webm/opus): the STT daemon's decode chain (soundfile → audioread)
 * cannot read webm/opus and the packaged backend ships no ffmpeg. Decoding
 * with the SAME browser that recorded the audio always succeeds
 * (`decodeAudioData` handles its own container), and PCM16 WAV is what the
 * FunASR side reads natively — the server keeps zero decode dependencies.
 *
 * Availability is probed once via the status route; `null` means unknown
 * (backend unreachable) and stays clickable so the first attempt can surface
 * the real failure. The daemon cold-starts on first use, so a `warming`
 * answer retries for up to a minute before giving up with a toast-able error.
 *
 * @module composables/use-voice-input
 */
import { onScopeDispose, ref } from 'vue';
// Explicit import (not an auto-import): bridge/* submodules are not
// auto-imported, and the state machine's bridge calls must be mockable from
// tests via vi.mock on a stable specifier.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchVoiceStatus, requestTranscription, TranscribeRejected } from '~/composables/bridge/voice';

/** Where the recording state machine stands. */
export type VoiceState = 'idle' | 'recording' | 'transcribing';

/** Why the last start/stop attempt failed (toast-able, mapped by the component). */
export type VoiceErrorKind = 'permission' | 'unsupported' | 'too_short' | 'warming' | 'unavailable' | 'failed';

/** The result of one stopRecording() round. */
export interface VoiceResult {
  ok: boolean;
  text?: string;
  reason?: VoiceErrorKind | 'idle';
}

/** Hard recording cap — a transcription that long would outlast its usefulness. */
export const MAX_RECORDING_SEC = 300;

/** Recordings shorter than this are noise, not speech; refuse before the request. */
export const MIN_AUDIO_SEC = 0.3;

/** The rate the STT model wants (SenseVoice trains at 16 kHz). */
export const TARGET_SAMPLE_RATE = 16000;

/** How long a cold start may keep the retry loop going (12 × 5 s). */
const WARMING_RETRY_LIMIT = 12;
const WARMING_RETRY_INTERVAL_MS = 5000;

/** MediaRecorder codec candidates, most portable first. */
const RECORDER_MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'];

/**
 * Encode float samples as a 16-bit PCM WAV (RIFF) byte array.
 * Pure and synchronous — pinned directly by unit tests.
 * @param samples Mono samples in [-1, 1].
 * @param sampleRate The sample rate the samples were captured at.
 * @returns The WAV byte array (44-byte RIFF header + PCM16 data).
 */
export function encodeWavPcm16(samples: Float32Array, sampleRate: number): Uint8Array {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeString = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeString(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, 'WAVE');
  writeString(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeString(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i] ?? 0));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Uint8Array(buffer);
}

/**
 * Average every channel of an AudioBuffer into one mono track.
 * @param buffer A decoded recording.
 * @returns One mono track of the same length.
 */
export function mixdownToMono(buffer: AudioBuffer): Float32Array {
  const length = buffer.length;
  const mono = new Float32Array(length);
  const channels = buffer.numberOfChannels;
  for (let ch = 0; ch < channels; ch++) {
    const data = buffer.getChannelData(ch);
    for (let i = 0; i < length; i++) mono[i] = (mono[i] ?? 0) + (data[i] ?? 0);
  }
  if (channels > 1) {
    for (let i = 0; i < length; i++) mono[i] = (mono[i] ?? 0) / channels;
  }
  return mono;
}

/**
 * Naive linear-interpolation resampler — voice quality, zero dependencies.
 * @param samples Source samples.
 * @param fromRate Source rate.
 * @param toRate Target rate (no-op when equal).
 * @returns The resampled track (the same array when the rates match).
 */
export function resampleLinear(samples: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate || samples.length === 0) return samples;
  const ratio = fromRate / toRate;
  const outLength = Math.max(1, Math.floor(samples.length / ratio));
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, samples.length - 1);
    const weight = position - left;
    const leftValue = samples[left] ?? 0;
    const rightValue = samples[right] ?? 0;
    out[i] = leftValue * (1 - weight) + rightValue * weight;
  }
  return out;
}

/**
 * The voice-input state machine. UI-free: callers map `lastError` to their own
 * copy and render `state` / `elapsedSec` however they like.
 * @returns The reactive state refs plus the start/stop/probe actions.
 */
export function useVoiceInput() {
  const state = ref<VoiceState>('idle');
  /** null = unknown (probe unreachable) — the button stays clickable. */
  const isSttAvailable = ref<boolean | null>(null);
  const elapsedSec = ref(0);
  const lastError = ref<VoiceErrorKind | null>(null);

  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let stopped: Promise<Blob | null> | null = null;
  let ticker: ReturnType<typeof setInterval> | null = null;
  let recordingStartedAt = 0;

  /** Availability probe: weights on disk gate the button; a cold daemon does not. */
  async function probeAvailability(): Promise<void> {
    const status = await fetchVoiceStatus();
    isSttAvailable.value = status === null ? null : status.model_dir_present;
  }

  function cleanupStream(): void {
    stream?.getTracks().forEach(track => track.stop());
    stream = null;
  }

  function clearTicker(): void {
    if (ticker !== null) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  function pickMimeType(): string | undefined {
    if (typeof MediaRecorder === 'undefined') return undefined;
    return RECORDER_MIME_CANDIDATES.find(mime => MediaRecorder.isTypeSupported(mime));
  }

  async function startRecording(): Promise<boolean> {
    if (state.value !== 'idle') return false;
    lastError.value = null;
    if (isSttAvailable.value === false) return false;
    if (
      typeof navigator === 'undefined' ||
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === 'undefined'
    ) {
      lastError.value = 'unsupported';
      return false;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
      });
    } catch {
      lastError.value = 'permission';
      return false;
    }
    const mimeType = pickMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch {
      cleanupStream();
      lastError.value = 'unsupported';
      return false;
    }
    chunks = [];
    const activeRecorder = recorder;
    activeRecorder.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    activeRecorder.onerror = () => {
      if (state.value === 'recording') void stopRecording();
    };
    // Capture the instance: stopRecording() nulls the module ref BEFORE the
    // browser fires onstop, and the closure must not read the ref.
    stopped = new Promise<Blob | null>(resolve => {
      activeRecorder.onstop = () => resolve(new Blob(chunks, { type: activeRecorder.mimeType || 'audio/webm' }));
    });
    activeRecorder.start(1000);
    state.value = 'recording';
    recordingStartedAt = Date.now();
    elapsedSec.value = 0;
    ticker = setInterval(() => {
      elapsedSec.value = Math.floor((Date.now() - recordingStartedAt) / 1000);
      if (elapsedSec.value >= MAX_RECORDING_SEC) void stopRecording();
    }, 250);
    return true;
  }

  async function toWav16k(blob: Blob): Promise<{ wav: Uint8Array; durationSec: number }> {
    const context = new AudioContext();
    try {
      const decoded = await context.decodeAudioData(await blob.arrayBuffer());
      const mono = mixdownToMono(decoded);
      const resampled = resampleLinear(mono, decoded.sampleRate, TARGET_SAMPLE_RATE);
      return { wav: encodeWavPcm16(resampled, TARGET_SAMPLE_RATE), durationSec: decoded.duration };
    } finally {
      void context.close();
    }
  }

  /**
   * Transcribe with the cold-start retry loop folded in (bounded ~60 s).
   * @param wav The encoded WAV payload.
   * @returns The recognized text.
   */
  async function transcribeWithWarmupRetry(wav: Uint8Array): Promise<string> {
    let attempts = 0;
    for (;;) {
      try {
        return await requestTranscription(wav);
      } catch (error) {
        const warming = error instanceof TranscribeRejected && error.kind === 'warming';
        attempts++;
        if (!warming || attempts >= WARMING_RETRY_LIMIT) {
          lastError.value =
            error instanceof TranscribeRejected && error.kind === 'unavailable' ? 'unavailable' : 'failed';
          throw error;
        }
        await new Promise(resolve => setTimeout(resolve, WARMING_RETRY_INTERVAL_MS));
      }
    }
  }

  async function stopRecording(): Promise<VoiceResult> {
    if (state.value !== 'recording' || !recorder) return { ok: false, reason: 'idle' };
    const recorderBeingStopped = recorder;
    recorder = null;
    clearTicker();
    state.value = 'transcribing';
    recorderBeingStopped.stop();
    const blob = stopped ? await stopped : null;
    stopped = null;
    cleanupStream();
    if (!blob || blob.size === 0) {
      state.value = 'idle';
      lastError.value = 'failed';
      return { ok: false, reason: 'failed' };
    }

    let wav: Uint8Array;
    let durationSec: number;
    try {
      ({ wav, durationSec } = await toWav16k(blob));
    } catch {
      state.value = 'idle';
      lastError.value = 'failed';
      return { ok: false, reason: 'failed' };
    }
    if (durationSec < MIN_AUDIO_SEC) {
      state.value = 'idle';
      lastError.value = 'too_short';
      return { ok: false, reason: 'too_short' };
    }

    try {
      const text = await transcribeWithWarmupRetry(wav);
      state.value = 'idle';
      return { ok: true, text };
    } catch {
      state.value = 'idle';
      return { ok: false, reason: lastError.value ?? 'failed' };
    }
  }

  /** Discard an in-flight recording (scope teardown, page hidden). */
  function cancelRecording(): void {
    if (state.value !== 'recording') return;
    const recorderBeingStopped = recorder;
    recorder = null;
    clearTicker();
    try {
      recorderBeingStopped?.stop();
    } catch {
      // An already-dead recorder must not mask the state reset below.
    }
    chunks = [];
    stopped = null;
    cleanupStream();
    state.value = 'idle';
  }

  onScopeDispose(cancelRecording);
  if (typeof document !== 'undefined') {
    const visibilityHandler = () => {
      if (document.hidden) cancelRecording();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    onScopeDispose(() => document.removeEventListener('visibilitychange', visibilityHandler));
  }

  return {
    state,
    isSttAvailable,
    elapsedSec,
    lastError,
    probeAvailability,
    startRecording,
    stopRecording
  };
}
