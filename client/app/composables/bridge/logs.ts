/**
 * Backend log reading and the live log WebSocket stream.
 *
 * @module bridge/logs
 */
import { teardownWebSocket } from './transport';
import { WS_BASE_URL } from '../env';
import { createWsMessageHandler } from '../ws-message';

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
    method: 'get'
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
  onError?: (e: string) => void
): { close: () => void } {
  const url = `${WS_BASE_URL}/logs/ws`;

  let socket: WebSocket | null = null;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    const s = socket;
    socket = null;
    teardownWebSocket(s);
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

  // Every parsed frame is forwarded regardless of its `event` field
  const handleLogFrame = createWsMessageHandler<LogStreamFrame>({});

  socket.onmessage = event => {
    const frame = handleLogFrame(event);
    if (frame) onFrame(frame);
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
