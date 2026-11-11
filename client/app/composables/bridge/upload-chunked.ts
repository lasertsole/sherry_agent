/**
 * Chunked media upload (ZCode parity).
 *
 * A payload larger than one chunk is split into `CHUNK_SIZE` pieces posted to
 * `/upload/chunk`; `/upload/complete` assembles them by content hash and
 * answers the served URL. A single-chunk payload keeps the whole-body
 * `/images|/audio|/video/upload` route — the chunk path's extra complete
 * round-trip buys nothing there.
 *
 * @module bridge/upload-chunked
 */

import { uploadBytesToUrl } from './upload';

/** One chunk; mirrors the server's `HTTP_UPLOAD["chunk_size_bytes"]`. */
export const CHUNK_SIZE = 512 * 1024;

/** Uploads above this skip the client SHA-256 cache (hashing is the cost). */
export const MAX_STAGED_BYTES = 64 * 1024 * 1024;

export type UploadProgress = (sent: number, total: number) => void;

/** Upload id accepted by the server's `[A-Za-z0-9_-]{1,64}` rule. */
function newUploadId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/**
 * Upload one payload, chunked when it spans more than one chunk.
 *
 * @param bytes       Raw payload bytes
 * @param kind        Media kind, drives the whole-body fallback endpoint
 * @param sessionId   Session that owns the upload (staging is session-scoped)
 * @param contentType MIME type recorded with the chunks
 * @param onProgress  Called after every accepted chunk with the sent/total bytes
 * @returns           The served URL
 */
export async function uploadChunked(
  bytes: Uint8Array<ArrayBuffer>,
  kind: 'image' | 'audio' | 'video',
  sessionId: string,
  contentType: string,
  onProgress?: UploadProgress
): Promise<string> {
  if (bytes.length <= CHUNK_SIZE) {
    return uploadBytesToUrl(kind, bytes, contentType);
  }

  const uploadId = newUploadId();
  const totalChunks = Math.ceil(bytes.length / CHUNK_SIZE);
  for (let index = 0; index < totalChunks; index++) {
    const chunk = bytes.subarray(index * CHUNK_SIZE, (index + 1) * CHUNK_SIZE);
    const query = new URLSearchParams({
      session_id: sessionId,
      upload_id: uploadId,
      chunk_index: String(index),
      total_chunks: String(totalChunks),
      filename: `${kind}-${uploadId}`,
      content_type: contentType
    });
    let response: Response;
    try {
      response = await fetchApiRaw({
        url: `/upload/chunk?${query.toString()}`,
        method: 'post',
        body: chunk,
        contentType
      });
    } catch (e) {
      throw new Error(`${kind} chunk ${index} upload network error: ${e}`, { cause: e });
    }
    if (!response.ok) {
      throw new Error(`${kind} chunk ${index} upload failed: HTTP ${response.status}`);
    }
    onProgress?.(Math.min((index + 1) * CHUNK_SIZE, bytes.length), bytes.length);
  }

  const completeQuery = new URLSearchParams({ session_id: sessionId, upload_id: uploadId });
  let complete: Response;
  try {
    complete = await fetchApiRaw({
      url: `/upload/complete?${completeQuery.toString()}`,
      method: 'post'
    });
  } catch (e) {
    throw new Error(`${kind} upload completion network error: ${e}`, { cause: e });
  }
  if (!complete.ok) {
    throw new Error(`${kind} upload completion failed: HTTP ${complete.status}`);
  }
  let data: { ok?: boolean; url?: string };
  try {
    data = await complete.json();
  } catch {
    throw new Error(`${kind} upload completion failed: server returned non-JSON response`);
  }
  if (!data.ok || !data.url) {
    throw new Error(`${kind} upload completion failed: ${JSON.stringify(data)}`);
  }
  return data.url;
}
