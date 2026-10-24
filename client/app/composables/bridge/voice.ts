/**
 * Voice-input transport: availability probe + WAV transcription through the
 * gateway (`GET /audio/transcribe/status`, `POST /audio/transcribe`).
 *
 * The browser cannot reach the local STT daemon (loopback only, no CORS/auth,
 * file-path contract) — the backend route is the proxy. Like `upload.ts`, this
 * module owns its failure contract on the raw transport and throws typed
 * errors; no toast and no retry live here.
 *
 * @module bridge/voice
 */

/** Availability snapshot from `GET /audio/transcribe/status`. */
export interface VoiceStatus {
  /** SenseVoice weights on disk — the configuration truth the mic button gates on. */
  model_dir_present: boolean;
  /** Daemon answering ready; false merely means cold (it spawns on first use). */
  daemon_ready: boolean;
}

/** Why a transcription request was refused or failed. */
export type TranscribeFailureKind = 'warming' | 'unavailable' | 'error';

/** A transcription rejection the voice state machine can branch on. */
export class TranscribeRejected extends Error {
  readonly kind: TranscribeFailureKind;

  constructor(kind: TranscribeFailureKind, message: string, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = 'TranscribeRejected';
    this.kind = kind;
  }
}

/**
 * Probe STT availability.
 * @returns The status snapshot, or null when the backend is unreachable — the
 * caller treats unknown as "let the first attempt surface the real error".
 */
export async function fetchVoiceStatus(): Promise<VoiceStatus | null> {
  let resp: Response;
  try {
    resp = await fetchApiRaw({ url: '/audio/transcribe/status' });
  } catch {
    return null;
  }
  if (!resp.ok) return null;
  try {
    const json = (await resp.json()) as Partial<VoiceStatus>;
    return {
      model_dir_present: json.model_dir_present === true,
      daemon_ready: json.daemon_ready === true
    };
  } catch {
    return null;
  }
}

/**
 * Transcribe one 16 kHz mono PCM WAV payload.
 *
 * The client-side abort budget (120 s) sits above the server's own daemon
 * timeout (60 s) + decode margin, so a slow CPU transcription fails on the
 * server's terms, not the transport's.
 *
 * @param wav PCM16 WAV bytes.
 * @returns The recognized text.
 * @throws TranscribeRejected `warming` (503) / `unavailable` (503) / `error`.
 */
export async function requestTranscription(wav: Uint8Array): Promise<string> {
  const timeoutSignal =
    typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
      ? AbortSignal.timeout(120_000)
      : undefined;
  let resp: Response;
  try {
    resp = await fetchApiRaw({
      url: '/audio/transcribe',
      method: 'post',
      body: wav as unknown as BodyInit,
      contentType: 'audio/wav',
      signal: timeoutSignal
    });
  } catch (e) {
    throw new TranscribeRejected('error', `voice transcription network error: ${e}`, { cause: e });
  }

  let json: { success?: boolean; text?: string; message?: string; warming?: boolean; unavailable?: boolean };
  try {
    json = await resp.json();
  } catch {
    throw new TranscribeRejected('error', `voice transcription failed: HTTP ${resp.status}`);
  }

  if (resp.ok && json.success && typeof json.text === 'string') return json.text;
  if (resp.status === 503 && json.warming) {
    throw new TranscribeRejected('warming', json.message || 'STT model is cold-starting');
  }
  if (resp.status === 503 && json.unavailable) {
    throw new TranscribeRejected('unavailable', json.message || 'STT model is not configured');
  }
  throw new TranscribeRejected('error', json.message || `voice transcription failed: HTTP ${resp.status}`);
}
