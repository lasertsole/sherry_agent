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
import type { HealthStatus } from '~/types/backend/HealthStatus';
import { fetchApi } from './requestApi';

/**
 * Chat 请求体 —— 供 sendChatMessage / handleSend 使用。
 * 字段与后端 `type/message.py` MultiModalMessage 保持一致。
 */
export interface ChatRequest {
  /** 会话 ID（缺省时后端按 "default"/默认会话处理） */
  session_id?: string;
  /** 文本内容 */
  text: string;
  /** 图片 base64 列表（Tauri 模式使用；浏览器模式下会自动上传转为 image_path_list） */
  image_base64_list?: string[];
  /** 图片 URL 列表（浏览器模式使用，来自 /images/upload 上传后返回的 HTTP URL） */
  image_path_list?: string[];
}

/**
 * 浏览器模式下后端 `/sessions/agent/ws` 返回的流式事件帧。
 * 对应 `server/trigger/ws/messages.py` 的 `{"event": ..., "session_id": ..., "content": ...}`。
 */
export type AgentWsEventType = 'chunk' | 'done' | 'error' | 'stopped' | 'hitl_request';

/** Chunk type — distinguishes conversational text from tool-call markers. */
export type AgentChunkType = 'text' | 'tool_start' | 'tool_end' | 'tool_result';

/** HITL interrupt payload sent by the server when the agent pauses for human approval. */
export interface HitlInterruptData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}

/** HITL decision sent by the client to resume the agent. */
export interface HitlResponse {
  decision: 'approve' | 'reject' | 'edit';
  message?: string;
  edited_args?: Record<string, unknown>;
}

export interface AgentWsEvent {
  event: AgentWsEventType;
  session_id?: string | null;
  content?: string;
  /** Chunk type (only present on "chunk" events). Defaults to "text" for backwards compat. */
  type?: AgentChunkType;
  /** Tool-call metadata (only present on "tool_result" chunks). */
  tool_id?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  error?: boolean;
}

/** Tauri stream-event payloads (mirror `src-tauri/src/commands/events.rs`). */
interface AgentStreamStart {
  session_id: string;
}
interface AgentStreamChunk {
  session_id: string;
  content: string;
  is_final?: boolean;
  /** Chunk type — "text" | "tool_start" | "tool_end" | "tool_result". Defaults to "text". */
  chunk_type?: AgentChunkType;
  /** Tool-call metadata (only present on "tool_result" chunks). */
  tool_id?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  error?: boolean;
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
 * Typed chunk callback: receives the text fragment, its semantic type, the
 * session id the chunk belongs to, and optional tool-call metadata (present
 * only on `tool_result` chunks). The session id lets the caller route chunks
 * to the correct per-session ChatPage when multiple sessions stream concurrently.
 */
export type OnChunkCallback = (
  content: string,
  type: AgentChunkType,
  sessionId: string,
  meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean },
) => void;

/** HITL interrupt callback: invoked when the agent pauses for human approval. */
export type OnHitlCallback = (data: HitlInterruptData) => void;

/**
 * Send a chat message and receive streaming chunks.
 *
 * In Tauri mode the chunks arrive via Tauri Events
 * (`agent:stream:chunk`). In browser mode the request streams over the
 * backend WebSocket (`/sessions/agent/ws`).
 *
 * @param request  The chat payload (session_id, text, images).
 * @param onChunk  Callback invoked for each text fragment with its type and session id.
 * @returns        Resolves when the stream completes; rejects on error.
 */
export async function sendChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
): Promise<void> {
  return streamChatMessage(request, onChunk).promise;
}

/** Tauri mode: invoke IPC + listen for Tauri Events. */
async function sendChatMessageTauri(
  request: ChatRequest,
  onChunk: OnChunkCallback,
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
      const p = event.payload;
      onChunk(p.content, p.chunk_type ?? 'text', p.session_id, {
        tool_id: p.tool_id,
        tool_name: p.tool_name,
        args: p.args,
        error: p.error,
      });
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
  /** Send a HITL decision (approve/reject/edit) to resume a pending agent. */
  sendHitlResponse?(response: HitlResponse): void;
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
 * @param onChunk  Called with each text fragment, its semantic type, and the session id.
 * @returns        `{ controller, promise }`.
 */
export function streamChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  if (isTauri()) {
    const promise = sendChatMessageTauri(request, onChunk);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'default') },
      promise,
    };
  }
  return sendChatMessageWs(request, onChunk, onHitl);
}

/**
 * Browser mode: stream agent chat over the backend WebSocket
 * (`/sessions/agent/ws`) instead of the (non-existent) SSE HTTP endpoint.
 *
 * 协议（参见 `server/trigger/ws/messages.py`）：
 * - 客户端先将 base64 图片通过 HTTP POST /images/upload 上传获取 URL，
 *   再通过 WebSocket 发送消息体：
 *   `{ session_id, multi_modal_message: { text, image_base64_list: [], image_path_list: [上传后的URL...] } }`
 * - 服务端返回 `{ event: "chunk", content }` 流式帧，
 *   以 `{ event: "done" }`（成功）或 `{ event: "error", content }` 结束。
 *
 * @param request  The chat payload.
 * @param onChunk  Called with each text fragment.
 * @returns        `{ controller, promise }` — `promise` resolves on completion.
 */
/**
 * 将 base64 图片上传到后端 /images/upload 并返回 URL 列表。
 *
 * @param base64List  base64 编码的图片字符串列表（可能带 data:...;base64, 前缀）
 * @param baseURL     后端 HTTP 基地址
 * @returns           上传后的图片 URL 数组（与输入同序）
 */
async function uploadImagesToUrls(base64List: string[], baseURL: string): Promise<string[]> {
  const urls: string[] = [];
  for (const base64 of base64List) {
    let contentType = 'image/png';
    let pureBase64 = base64;

    // 若带有 data:image/xxx;base64, 前缀，则剥离并提取 MIME 类型
    const match = pureBase64.match(/^data:(image\/[\w+-]+);base64,(.+)$/);
    if (match) {
      contentType = match[1];
      pureBase64 = match[2];
    }

    const bytes = Uint8Array.from(atob(pureBase64), (c) => c.charCodeAt(0));

    let resp: Response;
    try {
      resp = await fetch(`${baseURL}/images/upload`, {
        method: 'POST',
        headers: { 'Content-Type': contentType },
        body: bytes,
      });
    } catch (e) {
      throw new Error(`图片上传网络错误: ${e}`);
    }

    if (!resp.ok) {
      throw new Error(`图片上传失败: HTTP ${resp.status}`);
    }

    let json: { success?: boolean; url?: string; filename?: string };
    try {
      json = await resp.json();
    } catch {
      throw new Error('图片上传失败: 服务器返回非 JSON 响应');
    }

    if (!json.success || !json.url) {
      throw new Error(`图片上传失败: ${JSON.stringify(json)}`);
    }

    urls.push(json.url);
  }
  return urls;
}

function sendChatMessageWs(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/sessions/agent/ws`;
  const sessionId = request.session_id || 'default';

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
      if (done) return;
      done = true;
      if (socket) {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
        }
        closeSocket();
      }
      release('aborted');
    },
    sendHitlResponse: (response: HitlResponse) => {
      // 连接已关闭/中止时静默忽略
      if (done || !socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(
        JSON.stringify({
          type: 'hitl_response',
          session_id: sessionId,
          decision: response.decision,
          message: response.message ?? '',
          edited_args: response.edited_args,
        }),
      );
    },
  };

  const promise = new Promise<void>(async (resolve, reject) => {
    release = (err?: string) => {
      if (done && err === 'aborted') return;
      if (err) reject(new Error(err));
      else resolve();
    };

    // 将 base64 图片上传到后端 /images/upload，获取 HTTP URL
    let imageUrls: string[] = [];
    const imageList = request.image_base64_list;
    if (imageList && imageList.length > 0) {
      try {
        imageUrls = await uploadImagesToUrls(imageList, baseURL);
      } catch (e) {
        if (!done) {
          done = true;
          release(`图片上传失败: ${e}`);
        }
        return;
      }
    }

    // 上传期间被中止，不再建立 WebSocket
    if (done) return;

    try {
      socket = new WebSocket(url);
    } catch (e) {
      if (!done) {
        done = true;
        release(String(e));
      }
      return;
    }

    socket.onopen = () => {
      socket?.send(
        JSON.stringify({
          session_id: sessionId,
          multi_modal_message: {
            text: request.text || '',
            image_base64_list: [],
            image_path_list: imageUrls,
          },
        }),
      );
    };

    socket.onmessage = (event) => {
      let data: AgentWsEvent;
      try {
        data = JSON.parse(event.data as string) as AgentWsEvent;
      } catch {
        return;
      }
      if (data.event === 'chunk') {
        onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error,
        });
      } else if (data.event === 'hitl_request') {
        // HITL 中断：agent 需人工审批，调用 onHitl 回调（无回调时静默忽略）
        if (onHitl && data.content) {
          onHitl(data.content as unknown as HitlInterruptData);
        }
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
  });

  return { controller, promise };
}

/**
 * Resume a paused HITL agent over a fresh WebSocket, independent of any live
 * streaming socket or AbortController.
 *
 * Unlike `sendHitlResponse` (which is only attached to an in-flight stream
 * returned by `postAgentStream`), this opens a brand-new connection to
 * `/sessions/agent/ws` and sends a `hitl_response` frame. The backend handles
 * that frame before any message validation, so it resumes the paused agent
 * (`Command(resume=...)`) even after a session switch, page refresh, browser
 * restart, or server restart re-persisted the interrupt.
 *
 * Chunks streamed during the resumed execution are routed through `onChunk`
 * (same callback contract as `sendChatMessage`), so the ongoing output still
 * appears in the chat UI. A subsequent `hitl_request` (sequential HITL) is
 * forwarded to `onHitl` so the caller can re-show the approval card.
 *
 * @param sessionId  Session whose agent is paused at the HITL interrupt.
 * @param decision   `"approve"` | `"reject"` | `"edit"`.
 * @param message    Optional message accompanying the decision.
 * @param onChunk    Streamed chunk callback (text/tool_start/tool_end).
 * @param onHitl     Forwarded when the resumed run pauses again for approval.
 * @returns          `{ controller, promise }` — `promise` resolves on `done`,
 *                   rejects on `error`/teardown. `controller` can stop the run.
 */
export function resumeHitl(
  sessionId: string,
  decision: string,
  message: string = '',
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/sessions/agent/ws`;

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
      if (done) return;
      done = true;
      if (socket) {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
        }
        closeSocket();
      }
      release('aborted');
    },
  };

  const promise = new Promise<void>((resolve, reject) => {
    release = (err?: string) => {
      if (done && err === 'aborted') return;
      if (err) reject(new Error(err));
      else resolve();
    };

    try {
      socket = new WebSocket(url);
    } catch (e) {
      done = true;
      release(String(e));
      return;
    }

    socket.onopen = () => {
      socket?.send(
        JSON.stringify({
          type: 'hitl_response',
          session_id: sessionId,
          decision,
          message: message ?? '',
        }),
      );
    };

    socket.onmessage = (event) => {
      let data: AgentWsEvent;
      try {
        data = JSON.parse(event.data as string) as AgentWsEvent;
      } catch {
        return;
      }
      if (data.event === 'chunk') {
        onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error,
        });
      } else if (data.event === 'hitl_request') {
        // 顺序 HITL：恢复后的执行再次暂停，转发给调用方重新显示审批卡
        if (onHitl && data.content) {
          onHitl(data.content as unknown as HitlInterruptData);
        }
      } else if (data.event === 'done' || data.event === 'stopped') {
        if (!done) {
          done = true;
          closeSocket();
          release();
        }
      } else if (data.event === 'error') {
        if (!done) {
          done = true;
          closeSocket();
          release(data.content || 'WebSocket resume error');
        }
      }
    };

    socket.onerror = () => {
      if (!done) {
        done = true;
        closeSocket();
        release('WebSocket resume connection error');
      }
    };

    socket.onclose = () => {
      if (!done) {
        done = true;
        release('WebSocket resume closed before completion');
      }
    };
  });

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

/**
 * List all skills (builtin, auto, third_party).
 *
 * `fetchApi` resolves through Nuxt's `useFetch`, which dedupe/caches GET
 * requests by URL. A timestamp query param acts as a cache-buster so that
 * repeated calls after a curator run (or any lifecycle change) always hit the
 * network and return fresh data instead of a stale cached snapshot.
 */
export async function listSkills(): Promise<{ skills: SkillInfo[] }> {
  return fetchApi({
    url: '/skills',
    opts: { _ts: Date.now() },
    method: 'get',
  }) as unknown as Promise<{ skills: SkillInfo[] }>;
}

/**
 * Read a single skill's full SKILL.md content.
 */
export async function readSkill(location: string): Promise<SkillDetail> {
  const cleanPath = location.replace(/^\.\//, '');
  return fetchApi({ url: `/skills/${cleanPath}`, method: 'get' }) as unknown as Promise<SkillDetail>;
}

/** Auto-transition counters returned by `run_curator_review` (e.g. marked_stale/archived/reactivated/checked/seeded). */
export interface CuratorAutoTransitions {
  marked_stale: number;
  archived: number;
  reactivated: number;
  checked: number;
  seeded: number;
}

/** Mirrors the result object returned by `context_engine/curator/orchestrator.run_curator_review`. */
export interface CuratorRunResult {
  started_at: string;
  auto_transitions: CuratorAutoTransitions;
  /** Human-readable run summary (e.g. "no changes"). */
  summary_so_far: string;
  /** Present when the LLM consolidation pass failed (e.g. model not configured). */
  error?: string;
}

/** Response of `POST /curator/run`. */
export interface CuratorRunResponse {
  success: boolean;
  result?: CuratorRunResult;
  error?: string;
}

/**
 * Force-trigger a curator review/maintenance run against the auto-learned
 * skills. Calls `run_curator_review` on the backend (the forced entry point,
 * not the idle-scheduled `maybe_run_curator`), so it always executes.
 *
 * @returns `{ success, result, error }` from the backend.
 */
export async function runCuratorReview(): Promise<CuratorRunResponse> {
  return fetchApi({ url: '/curator/run', method: 'post' }) as unknown as Promise<CuratorRunResponse>;
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

// ── Skills ───────────────────────────────────────────────

export interface SkillInfo {
  name: string;
  description: string;
  location: string;
  category: 'builtin' | 'auto' | 'third_party';
}

export interface SkillDetail extends SkillInfo {
  content: string;
}

// ── Logs ────────────────────────────────────────────────

/** Log level severity, matching the backend log format. */
export type LogLevel = 'TRACE' | 'DEBUG' | 'INFO' | 'SUCCESS' | 'WARNING' | 'ERROR' | 'CRITICAL';

/** Metadata for a single `.log` file on the backend. */
export interface LogFileInfo {
  name: string;
  path: string;
  size: number;
  modified: string;
  is_error: boolean;
  /** True when this is the log written by the currently running backend process. */
  is_current: boolean;
}

/** Response of `GET /logs/files`. */
export interface LogFileList {
  files: LogFileInfo[];
}

/** Response of `GET /logs?path=...&lines=...`. */
export interface LogReadResult {
  success: boolean;
  path: string;
  content: string;
  lines: number;
}

/** A single log record pushed over the live WebSocket. */
export interface LogStreamData {
  timestamp: string;
  level: string;
  name: string;
  function: string;
  line: number;
  message: string;
}

/** A frame pushed by the `/logs/ws` WebSocket. */
export interface LogStreamFrame {
  event: string;
  data?: LogStreamData;
}

/**
 * List all available `.log` files, newest first.
 */
export async function listLogFiles(): Promise<LogFileList> {
  return fetchApi({ url: '/logs/files', method: 'get' }) as unknown as Promise<LogFileList>;
}

/**
 * Read the trailing `lines` of a log file (default 500).
 *
 * @param path  The log file path (from `LogFileInfo.path`).
 * @param lines Number of trailing lines to read.
 */
export async function readLogFile(path: string, lines?: number): Promise<LogReadResult> {
  return fetchApi({
    url: '/logs',
    opts: { path, lines: lines ?? 500 },
    method: 'get',
  }) as unknown as Promise<LogReadResult>;
}

/**
 * Open a live log stream over the backend WebSocket (`/logs/ws`).
 *
 * The server pushes JSON text frames: `{"event": "ready"}` on connect and
 * `{"event": "log", "data": {...}}` for each new log record.
 *
 * @param onFrame Called with each parsed frame.
 * @param onError Called with an error message on connection failure.
 * @returns       A handle whose `close()` tears down the socket.
 */
export function openLogStream(
  onFrame: (frame: LogStreamFrame) => void,
  onError?: (e: string) => void,
): { close: () => void } {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/logs/ws`;

  let socket: WebSocket | null = null;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
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

  try {
    socket = new WebSocket(url);
  } catch (e) {
    onError?.(String(e));
    return { close };
  }

  socket.onopen = () => {
    // No action needed; the server pushes a "ready" frame on connect.
  };

  socket.onmessage = (event) => {
    let frame: LogStreamFrame;
    try {
      frame = JSON.parse(event.data as string) as LogStreamFrame;
    } catch {
      return;
    }
    onFrame(frame);
  };

  socket.onerror = () => {
    onError?.('WebSocket connection error');
  };

  socket.onclose = () => {
    if (!closed) {
      closed = true;
      onError?.('WebSocket closed');
    }
  };

  return { close };
}
