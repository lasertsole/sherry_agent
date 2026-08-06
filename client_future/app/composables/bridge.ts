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

import type { HistoryMessage } from '~/types/backend/HistoryMessage';
import type { PromptFileResponse } from '~/types/backend/PromptFileResponse';
import type { CharacterResponse } from '~/types/backend/CharacterResponse';
import type { HealthStatus } from '~/types/backend/HealthStatus';
import { fetchApi } from './requestApi';

/**
 * Chat 请求体 —— 供 sendChatMessage / handleSend 使用。
 * 字段与后端 `type/message.py` MultiModalMessage 保持一致。
 */
export interface ChatRequest {
  /** 会话 ID（缺省时后端按 "main"/默认会话处理） */
  session_id?: string;
  /** 文本内容 */
  text: string;
  /** 图片 base64 列表 */
  image_base64_list?: string[];
}

/**
 * 浏览器模式下后端 `/sessions/agent/ws` 返回的流式事件帧。
 * 对应 `server/trigger/ws/messages.py` 的 `{"event": ..., "session_id": ..., "content": ...}`。
 */
export type AgentWsEventType = 'chunk' | 'done' | 'error' | 'stopped';

export interface AgentWsEvent {
  event: AgentWsEventType;
  session_id?: string | null;
  content?: string;
}

/** Tauri stream-event payloads (mirror `src-tauri/src/commands/events.rs`). */
interface AgentStreamStart {
  session_id: string;
}
interface AgentStreamChunk {
  session_id: string;
  content: string;
  is_final?: boolean;
}
interface AgentStreamEnd {
  session_id: string;
  content: string;
}
interface AgentStreamError {
  session_id: string;
  code: number;
  message: string;
}
interface ChatChunk {
  id: string;
  role: string;
  content: string;
}

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
 * (`agent:stream:chunk`). In browser mode the request streams over the
 * backend WebSocket (`/sessions/agent/ws`).
 *
 * @param request  The chat payload (session_id, text, images).
 * @param onChunk  Callback invoked for each text fragment.
 * @returns        Resolves when the stream completes; rejects on error.
 */
export async function sendChatMessage(
  request: ChatRequest,
  onChunk: (text: string) => void,
): Promise<void> {
  return streamChatMessage(request, onChunk).promise;
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

/**
 * Resolve the WebSocket base URL from an HTTP API base URL.
 *
 * `http://host:port` -> `ws://host:port`, `https://` -> `wss://`,
 * and strips any trailing slashes.
 */
function resolveWsBaseUrl(apiBaseUrl: string): string {
  return apiBaseUrl.replace(/^https?:\/\//, (m) => (m === 'https://' ? 'wss://' : 'ws://')).replace(/\/+$/, '');
}

/**
 * A handle that can be used to stop an ongoing generation request.
 *
 * `abort()` instructs the backend to halt the stream for the session and
 * tears down the underlying WebSocket. `closed` becomes `true` once torn down.
 */
export interface StreamController {
  /** Whether the connection has been closed/stopped/errored. */
  readonly closed: boolean;
  /** Stop the generation and close the connection. */
  abort(): void;
}

/**
 * High-level streamed agent chat, following the unified bridge convention.
 *
 * - **Tauri**: invokes `agent_chat` IPC and consumes `agent:stream:*` Tauri Events.
 * - **Browser**: opens a WebSocket to `/sessions/agent/ws`.
 *
 * Returns a `StreamController` whose `abort()` stops the generation, plus a
 * Promise that resolves when the stream completes (`done`) and rejects on
 * error or unexpected teardown.
 *
 * @param request  The chat payload.
 * @param onChunk  Called with each text fragment.
 * @returns        `{ controller, promise }`.
 */
export function streamChatMessage(
  request: ChatRequest,
  onChunk: (text: string) => void,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  if (isTauri()) {
    const promise = sendChatMessageTauri(request, onChunk);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'main') },
      promise,
    };
  }
  return sendChatMessageWs(request, onChunk);
}

/**
 * Browser mode: stream agent chat over the backend WebSocket
 * (`/sessions/agent/ws`) instead of the (non-existent) SSE HTTP endpoint.
 *
 * Protocol (see `server/trigger/ws/messages.py`):
 * - Client sends `{ session_id, multi_modal_message: { text, image_base64_list } }`
 * - Server replies with a stream of `{ event: "chunk", content }` frames,
 *   terminated by `{ event: "done" }` (success) or `{ event: "error", content }`.
 *
 * @param request  The chat payload.
 * @param onChunk  Called with each text fragment.
 * @returns        `{ controller, promise }` — `promise` resolves on completion.
 */
function sendChatMessageWs(
  request: ChatRequest,
  onChunk: (text: string) => void,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/sessions/agent/ws`;
  const sessionId = request.session_id || 'main';

  let socket: WebSocket | null = null;
  let done: boolean = false;
  let release: (err?: string) => void = () => {};

  const closeSocket = () => {
    const s = socket;
    socket = null;
    if (s) {
      s.onopen = null;
      s.onmessage = null;
      s.onerror = null;
      s.onclose = null;
      if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
        s.close();
      }
    }
  };

  const controller: StreamController = {
    get closed() {
      return done;
    },
    abort: () => {
      if (done || !socket) return;
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
      }
      done = true;
      closeSocket();
      release('aborted');
    },
  };

  const promise = new Promise<void>((resolve, reject) => {
    release = (err?: string) => {
      if (done && err === 'aborted') return; // aborts already handled at caller
      if (err) reject(new Error(err));
      else resolve();
    };
  });

  try {
    socket = new WebSocket(url);
  } catch (e) {
    release(String(e));
    return { controller, promise };
  }

  socket.onopen = () => {
    socket?.send(
      JSON.stringify({
        session_id: sessionId,
        multi_modal_message: {
          text: request.text || '',
          image_base64_list: request.image_base64_list || [],
        },
      }),
    );
  };

  socket.onmessage = (event) => {
    let data: AgentWsEvent;
    try {
      data = JSON.parse(event.data as string) as AgentWsEvent;
    } catch {
      return; // ignore non-JSON frames
    }
    if (data.event === 'chunk') {
      onChunk(data.content ?? '');
    } else if (data.event === 'done') {
      if (!done) {
        done = true;
        closeSocket();
        release();
      }
    } else if (data.event === 'error') {
      if (!done) {
        done = true;
        closeSocket();
        release(data.content || 'WebSocket stream error');
      }
    }
    // 'stopped' frames are consumed by the stop flow and ignored here.
  };

  socket.onerror = () => {
    if (!done) {
      done = true;
      closeSocket();
      release('WebSocket connection error');
    }
  };

  socket.onclose = () => {
    if (!done) {
      done = true;
      release('WebSocket closed before stream completion');
    }
  };

  return { controller, promise };
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
    await stopChatMessageBrowser(sessionId);
  }
}

/**
 * Browser mode: send a stop command over the agent WebSocket
 * (`/sessions/agent/ws`) instead of relying on the legacy HTTP stop endpoint.
 */
function stopChatMessageBrowser(sessionId: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const baseURL: string = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
    const wsBase = baseURL.replace(/^https?:\/\//, (m) => (m === 'https://' ? 'wss://' : 'ws://'));
    const url = `${wsBase.replace(/\/+$/, '')}/sessions/agent/ws`;

    let socket: WebSocket | null = null;

    try {
      socket = new WebSocket(url);
    } catch (e) {
      reject(e);
      return;
    }

    // Resolve once the server acknowledges the stop.
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data as string);
        if (data && data.event === 'stopped' && data.session_id === sessionId) {
          cleanup();
          resolve();
        }
      } catch {
        // ignore non-JSON frames
      }
    };

    socket.onopen = () => {
      socket?.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
    };

    socket.onerror = () => {
      cleanup();
      reject(new Error(`WebSocket stop failed: ${url}`));
    };

    socket.onclose = () => {
      cleanup();
      reject(new Error('WebSocket closed before stop confirmation'));
    };

    const cleanup = () => {
      if (socket) {
        socket.onmessage = null;
        socket.onopen = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        socket = null;
      }
    };
  });
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
