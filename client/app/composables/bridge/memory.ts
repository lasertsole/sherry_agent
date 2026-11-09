/**
 * Heartbeat file access (HEARTBEAT.md).
 *
 * Long-term memory has its own entry API in ``bridge/memory-entries.ts``; the
 * backend still exposes the legacy ``GET/PUT /memory`` shape for old clients,
 * but this app no longer calls it.
 *
 * @module bridge/memory
 */

/**
 * Read the heartbeat file (`workspace/HEARTBEAT.md`).
 *
 * Unlike memory, heartbeat deliberately skips Rust/Tauri and always goes
 * through `fetchApi` in both modes (there is no Rust command for heartbeat).
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless.
 */
export async function readHeartbeat(): Promise<Record<string, string>> {
  return fetchApiPayload<Record<string, string>>({
    url: '/heartbeat',
    opts: { _ts: Date.now() },
    method: 'get'
  });
}

/**
 * Overwrite the heartbeat file (`workspace/HEARTBEAT.md`, full replacement).
 * Always uses `fetchApi` in both modes — no Rust/Tauri command exists for
 * heartbeat.
 * @param fileToContent
 */
export async function writeHeartbeat(fileToContent: Record<string, string>): Promise<void> {
  await fetchApi({
    url: '/heartbeat',
    opts: { file_to_content: fileToContent },
    method: 'put'
  });
}

/** Global heartbeat switch state (the Heartbeat panel's toggle). */
export interface HeartbeatStatus {
  /** The persisted choice — what the switch shows. */
  enabled: boolean;
  /** The live scheduler state (differs briefly right after a toggle). */
  running: boolean;
  /** Tick interval in seconds. */
  interval_s: number;
}

/**
 * Read the global heartbeat switch state (`GET /heartbeat/status`).
 */
export async function fetchHeartbeatStatus(): Promise<HeartbeatStatus> {
  const res = await fetchApiPayload<HeartbeatStatus>({
    url: '/heartbeat/status',
    opts: { _ts: Date.now() },
    method: 'get'
  });
  return normalizeHeartbeatStatus(res);
}

/**
 * Toggle the global heartbeat scheduler (`PUT /heartbeat/status`).
 *
 * Applies immediately on the running service and persists to `sherry.jsonc`, so
 * the choice survives a restart. Rejects when the backend refuses (e.g. the
 * config write failed) — the caller reverts its optimistic switch.
 * @param enabled
 */
export async function setHeartbeatEnabled(enabled: boolean): Promise<HeartbeatStatus> {
  const res = await fetchApiPayload<HeartbeatStatus & { success?: boolean; message?: string }>({
    url: '/heartbeat/status',
    opts: { enabled },
    method: 'put'
  });
  if (res && res.success === false) {
    throw new Error(res.message || 'heartbeat toggle failed');
  }
  return normalizeHeartbeatStatus(res);
}

/**
 * Coerce a status payload into the typed shape (the switch needs a definite
 * boolean, and a missing field must not read as "off").
 * @param raw
 */
function normalizeHeartbeatStatus(
  raw: (Partial<HeartbeatStatus> & { data?: Partial<HeartbeatStatus> }) | null
): HeartbeatStatus {
  const source = raw?.data ?? raw ?? {};
  return {
    enabled: source.enabled === true,
    running: source.running === true,
    interval_s: Number(source.interval_s ?? 0)
  };
}
