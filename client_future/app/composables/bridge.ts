/**
 * Unified communication bridge between the Nuxt frontend and the backend.
 *
 * Supports two runtime modes:
 * - **Tauri desktop**: IPC via `invoke()` + Tauri Events for streaming
 * - **Browser dev**: direct HTTP to the Python backend via `fetchApi()`
 *
 * All API calls go through this module so that components never
 * need to know which transport layer is active.
 *
 * @module bridge
 */

import type { ChatRequest } from '~/types/backend/ChatRequest';
import type { ChatChunk } from '~/types/backend/ChatChunk';
import type { HistoryMessage } from '~/types/backend/HistoryMessage';
import type { PromptFileResponse } from '~/types/backend/PromptFileResponse';
import type { CharacterResponse } from '~/types/backend/CharacterResponse';
import type { HealthStatus } from '~/types/backend/HealthStatus';
import type { AgentStreamChunk } from '~/types/backend/AgentStreamChunk';
import type { AgentStreamEnd } from '~/types/backend/AgentStreamEnd';
import type { AgentStreamError } from '~/types/backend/AgentStreamError';
import type { AgentStreamStart } from '~/types/backend/AgentStreamStart';
import { fetchApi } from './requestApi';

// ── Runtime detection ────────────────────────────────────

/**
 * Returns `true` when running inside the Tauri desktop shell.
 */
function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

// ── Lazy Tauri imports ───────────────────────────────────

async function getInvoke() {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke;
}

async function getListen() {
  const { listen } = await import('@tauri-apps/api/event');
  return listen;
}

// ── Agent chat (streaming) ───────────────────────────────

/**
 * Send a chat message and receive streaming chunks.
 *
 * In Tauri mode the chunks arrive via Tauri Events
 * (`agent:stream:chunk`). In browser mode the SSE stream
 * is consumed directly from the Python backend.
 *
 * @param request  The chat payload (session_id, text, images).
 * @param onChunk  Callback invoked for each text fragment.
 * @returns        Resolves when the stream completes.
 */
export async function sendChatMessage(
  request: ChatRequest,
  onChunk: (text: string) => void,
): Promise<void> {
  if (isTauri()) {
    return sendChatMessageTauri(request, onChunk);
  }
  return sendChatMessageBrowser(request, onChunk);
}

/** Tauri mode: invoke IPC + listen for Tauri Events. */
async function sendChatMessageTauri(
  request: ChatRequest,
  onChunk: (text: string) => void,
): Promise<void> {
  const invoke = await getInvoke();
  const listen = await getListen();

  return new Promise<void>((resolve, reject) => {
    let unlistenChunk: (() => void) | null = null;
    let unlistenEnd: (() => void) | null = null;
    let unlistenErr: (() => void) | null = null;
    let unlistenStart: (() => void) | null = null;

    const cleanup = () => {
      unlistenChunk?.();
      unlistenEnd?.();
      unlistenErr?.();
      unlistenStart?.();
    };

    // Listen for stream start
    listen<AgentStreamStart>('agent:stream:start', () => {
      // Stream started, no action needed
    }).then((fn) => { unlistenStart = fn; });

    // Listen for chunks
    listen<AgentStreamChunk>('agent:stream:chunk', (event) => {
      onChunk(event.payload.content);
    }).then((fn) => { unlistenChunk = fn; });

    // Listen for stream end
    listen<AgentStreamEnd>('agent:stream:end', () => {
      cleanup();
      resolve();
    }).then((fn) => { unlistenEnd = fn; });

    // Listen for stream error
    listen<AgentStreamError>('agent:stream:error', (event) => {
      cleanup();
      reject(new Error(`[${event.payload.code}] ${event.payload.message}`));
    }).then((fn) => { unlistenErr = fn; });

    // Invoke the Rust command (triggers the SSE bridge)
    invoke<ChatChunk[]>('agent_chat', { request }).catch((err) => {
      cleanup();
      reject(err);
    });
  });
}

/** Browser mode: direct SSE fetch to the Python backend. */
async function sendChatMessageBrowser(
  request: ChatRequest,
  onChunk: (text: string) => void,
): Promise<void> {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';

  const response = await fetch(`${baseURL}/sessions/agent/sse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: request.session_id,
      multi_modal_message: {
        text: request.text || '',
        image_base64_list: request.image_base64_list || [],
      },
    }),
  });

  if (!response.ok) {
    throw new Error(`SSE request failed: HTTP ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error('response body is not readable');

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('data: ')) {
        onChunk(trimmed.slice(6));
      } else if (trimmed === 'data:') {
        onChunk('');
      }
    }
  }
}

// ── Stop generation ──────────────────────────────────────

/**
 * Stop an ongoing agent generation.
 */
export async function stopChatMessage(sessionId: string): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('agent_stop', { request: { session_id: sessionId } });
  } else {
    await fetchApi({
      url: '/sessions/agent/sse/stop',
      opts: { session_id: sessionId },
      method: 'post',
    });
  }
}

// ── Session ──────────────────────────────────────────────

/**
 * Clear all state for a session.
 */
export async function clearSession(sessionId: string): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('session_clear', { request: { session_id: sessionId } });
  } else {
    await fetchApi({
      url: '/sessions',
      opts: { session_id: sessionId },
      method: 'delete',
    });
  }
}

/**
 * Retrieve conversation history.
 */
export async function getHistory(
  sessionId: string,
  lastTurnCount: number = 10,
): Promise<HistoryMessage[]> {
  if (isTauri()) {
    const invoke = await getInvoke();
    return invoke<HistoryMessage[]>('session_history', {
      request: { session_id: sessionId, last_turn_count: lastTurnCount },
    });
  }
  return fetchApi({
    url: '/n_turns_history_messages',
    opts: { session_id: sessionId, last_turn_count: lastTurnCount },
    method: 'get',
  }) as unknown as Promise<HistoryMessage[]>;
}

// ── System Prompt ────────────────────────────────────────

/**
 * Read all system prompt files.
 */
export async function readSystemPrompt(): Promise<Record<string, string>> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<PromptFileResponse>('system_prompt_read');
    return resp.file_to_content;
  }
  return fetchApi({ url: '/system_prompt', method: 'get' }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite system prompt files (full replacement).
 */
export async function writeSystemPrompt(
  fileToContent: Record<string, string>,
): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('system_prompt_write', { payload: { file_to_content: fileToContent } });
  } else {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put',
    });
  }
}

/**
 * Partially update system prompt files (merge).
 */
export async function updateSystemPrompt(
  fileToContent: Record<string, string>,
): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('system_prompt_update', { payload: { file_to_content: fileToContent } });
  } else {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put',
    });
  }
}

// ── Character ────────────────────────────────────────────

type CharacterData = Record<string, Record<string, string>>;

/**
 * Read character configuration.
 */
export async function readCharacter(): Promise<CharacterData> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<CharacterResponse>('character_read');
    return resp.character_data;
  }
  return fetchApi({ url: '/character', method: 'get' }) as unknown as Promise<CharacterData>;
}

/**
 * Overwrite character configuration.
 */
export async function writeCharacter(data: CharacterData): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('character_write', { payload: { character_data: data } });
  } else {
    await fetchApi({
      url: '/character',
      opts: { character_data: data },
      method: 'put',
    });
  }
}

/**
 * Partially update character configuration (merge).
 */
export async function updateCharacter(data: CharacterData): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('character_update', { payload: { character_data: data } });
  } else {
    await fetchApi({
      url: '/character',
      opts: { character_data: data },
      method: 'put',
    });
  }
}

// ── Health ───────────────────────────────────────────────

/**
 * Check whether the Python backend is reachable.
 */
export async function checkHealth(): Promise<HealthStatus> {
  if (isTauri()) {
    const invoke = await getInvoke();
    return invoke<HealthStatus>('system_health');
  }
  // Browser fallback: try to fetch a lightweight endpoint
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  try {
    const resp = await fetch(`${baseURL}/system_prompt`);
    if (resp.ok) {
      return { healthy: true, message: 'Python backend reachable' };
    }
    return { healthy: false, message: `HTTP ${resp.status}` };
  } catch (e) {
    return { healthy: false, message: String(e) };
  }
}
