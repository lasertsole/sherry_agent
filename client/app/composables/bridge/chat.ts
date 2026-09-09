/**
 * Agent chat entry points and the streaming transport strategy.
 *
 * `createTransport()` picks the Tauri IPC transport or the browser WebSocket
 * transport at runtime; every chat call goes through it so callers never need
 * to know which transport layer is active.
 *
 * @module bridge/chat
 */
import type {
  AgentChunkType,
  ChatRequest,
  OnChunkCallback,
  OnDoneCallback,
  OnHitlCallback,
  OnQueuedCallback,
  StreamController
} from './chat-types';
import { getInvoke, getListen, isTauri } from './transport';
import { WS_BASE_URL } from '../env';
import { sendChatMessageWs } from './ws-stream';
import { createWsMessageHandler } from '../ws-message';

/** Tauri stream-event payloads (mirror `src-tauri/src/commands/events.rs`). */
interface AgentStreamStart {
  session_id: string;
}
interface AgentStreamChunk {
  session_id: string;
  content: string;
  is_final?: boolean;
  /** Chunk type — "text" | "reasoning" | "tool_start" | "tool_end" | "tool_result". Defaults to "text". */
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
export async function sendChatMessage(request: ChatRequest, onChunk: OnChunkCallback): Promise<void> {
  return streamChatMessage(request, onChunk).promise;
}

/**
 * Tauri mode: invoke IPC + listen for Tauri Events.
 * @param request
 * @param onChunk
 * @param onDone
 */
async function sendChatMessageTauri(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onDone?: OnDoneCallback
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
    }).then(fn => {
      unlistenStart = fn;
    });

    // Listen for chunks
    listen<AgentStreamChunk>('agent:stream:chunk', event => {
      const p = event.payload;
      onChunk(p.content, p.chunk_type ?? 'text', p.session_id, {
        tool_id: p.tool_id,
        tool_name: p.tool_name,
        args: p.args,
        error: p.error
      });
    }).then(fn => {
      unlistenChunk = fn;
    });

    // Listen for stream end
    listen<AgentStreamEnd>('agent:stream:end', () => {
      cleanup();
      onDone?.();
      resolve();
    }).then(fn => {
      unlistenEnd = fn;
    });

    // Listen for stream error
    listen<AgentStreamError>('agent:stream:error', event => {
      cleanup();
      reject(new Error(`[${event.payload.code}] ${event.payload.message}`));
    }).then(fn => {
      unlistenErr = fn;
    });

    // Invoke the Rust command (triggers the SSE bridge)
    invoke<ChatChunk[]>('agent_chat', { request }).catch(err => {
      cleanup();
      reject(err);
    });
  });
}

/** Per-event callbacks handed to a transport's `stream()`. */
export interface ChatStreamCallbacks {
  onChunk: OnChunkCallback;
  onHitl?: OnHitlCallback;
  onDone?: OnDoneCallback;
  onQueued?: OnQueuedCallback;
}

/**
 * Streaming transport strategy: encapsulates the runtime-specific way to start
 * a chat stream and to stop a generation, so call sites never branch on the
 * runtime themselves.
 */
export interface TransportStrategy {
  /** Stream a chat request to completion. */
  stream(
    request: ChatRequest,
    callbacks: ChatStreamCallbacks
  ): { controller: StreamController; promise: Promise<void> };
  /** Stop the session's ongoing generation. */
  stop(sessionId: string): Promise<void>;
}

/** Tauri desktop transport: `agent_chat` IPC + `agent:stream:*` Tauri Events. */
export class TauriTransport implements TransportStrategy {
  stream(
    request: ChatRequest,
    callbacks: ChatStreamCallbacks
  ): {
    controller: StreamController;
    promise: Promise<void>;
  } {
    const promise = sendChatMessageTauri(request, callbacks.onChunk, callbacks.onDone);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'default') },
      promise
    };
  }

  async stop(sessionId: string): Promise<void> {
    const invoke = await getInvoke();
    await invoke('agent_stop', { request: { session_id: sessionId } });
  }
}

/** Browser transport: direct WebSocket to `/sessions/agent/ws`. */
export class BrowserTransport implements TransportStrategy {
  stream(
    request: ChatRequest,
    callbacks: ChatStreamCallbacks
  ): {
    controller: StreamController;
    promise: Promise<void>;
  } {
    return sendChatMessageWs(request, callbacks.onChunk, callbacks.onHitl, callbacks.onDone, callbacks.onQueued);
  }

  stop(sessionId: string): Promise<void> {
    return stopChatMessageBrowser(sessionId);
  }
}

/**
 * Build the streaming transport for the current runtime (Strategy + Factory):
 * every chat call resolves its transport through this single seam instead of
 * branching on `isTauri()` itself.
 */
export function createTransport(): TransportStrategy {
  return isTauri() ? new TauriTransport() : new BrowserTransport();
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
 * @param onHitl   Called when the agent pauses for a human-in-the-loop decision.
 * @param onDone   Called once after the stream finishes (success or handled stop).
 * @param onQueued Called when the backend enqueues the message (session busy) instead of streaming immediately.
 * @returns        `{ controller, promise }`.
 */
export function streamChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
  onDone?: OnDoneCallback,
  onQueued?: OnQueuedCallback
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  return createTransport().stream(request, { onChunk, onHitl, onDone, onQueued });
}

/**
 * Stop an ongoing agent generation.
 * @param sessionId
 */
export async function stopChatMessage(sessionId: string): Promise<void> {
  await createTransport().stop(sessionId);
}

/**
 * Browser mode: send a stop command over the agent WebSocket
 * (`/sessions/agent/ws`) instead of relying on the legacy HTTP stop endpoint.
 * @param sessionId
 */
function stopChatMessageBrowser(sessionId: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const url = `${WS_BASE_URL}/sessions/agent/ws`;

    let socket: WebSocket | null = null;

    try {
      socket = new WebSocket(url);
    } catch (e) {
      reject(e);
      return;
    }

    // Resolve once the server acknowledges the stop.
    const handleStopFrame = createWsMessageHandler<{ event?: string; session_id?: string }>({
      stopped: data => {
        if (data.session_id === sessionId) {
          cleanup();
          resolve();
        }
      }
    });

    socket.onmessage = event => {
      try {
        handleStopFrame(event);
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
