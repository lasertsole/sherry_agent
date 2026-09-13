/**
 * Long-term memory and heartbeat file access (workspace/memory/*, HEARTBEAT.md).
 *
 * @module bridge/memory
 */
import type { PromptFileResponse } from '~/types/backend/PromptFileResponse';
import { invokeNative } from './transport';

/**
 * Read all long-term memory files (workspace/memory/*).
 *
 * Note: `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests. The timestamp query param is a legacy cache-buster from the
 * previous `useFetch`-based implementation and is now harmless (kept so the
 * backend URL shape is unchanged).
 */
export async function readMemory(): Promise<Record<string, string>> {
  const native = await invokeNative<PromptFileResponse>('memory_read');
  if (native !== null) return native.value.file_to_content;
  return fetchApi({
    url: '/memory',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite long-term memory files (full replacement).
 * Only provided files are overwritten; others are left unchanged.
 * @param fileToContent
 */
export async function writeMemory(fileToContent: Record<string, string>): Promise<void> {
  const native = await invokeNative('memory_write', { payload: { file_to_content: fileToContent } });
  if (native === null) {
    await fetchApi({
      url: '/memory',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}

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
  return fetchApi({
    url: '/heartbeat',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
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
