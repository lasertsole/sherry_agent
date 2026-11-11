/**
 * Short-TTL upload dedupe.
 *
 * The payload is hashed with SHA-256 and looked up in the Dexie `uploadCache`
 * table: the same bytes sent twice inside the TTL reuse the first URL instead of
 * re-uploading. The cache is best-effort — a blocked IndexedDB or a browser
 * without `crypto.subtle` simply uploads (and the upload itself never fails
 * because of the cache).
 *
 * @module bridge/upload-cache
 */
import { db } from '../db';
import type { UploadMediaKind } from './upload';
import { MAX_STAGED_BYTES, uploadChunked } from './upload-chunked';

/** Mirrors the server's `HTTP_UPLOAD["read_cache_ttl_ms"]`. */
export const UPLOAD_CACHE_TTL_MS = 30_000;

/**
 * SHA-256 of the payload as lowercase hex, or `null` when the platform cannot
 * hash it (or it is too large for the staging budget).
 * @param bytes Payload bytes
 */
async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string | null> {
  if (bytes.length > MAX_STAGED_BYTES) return null;
  try {
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest))
      .map(byte => byte.toString(16).padStart(2, '0'))
      .join('');
  } catch {
    return null;
  }
}

/**
 * Upload media, reusing a recent URL for byte-identical payloads.
 *
 * @param bytes       Raw payload bytes
 * @param kind        Media kind
 * @param sessionId   Session that owns the upload
 * @param contentType MIME type sent with the upload
 * @returns           The served URL
 */
export async function uploadWithCache(
  bytes: Uint8Array<ArrayBuffer>,
  kind: UploadMediaKind,
  sessionId: string,
  contentType: string
): Promise<string> {
  const hash = await sha256Hex(bytes);
  if (hash) {
    try {
      const cached = await db.uploadCache.where('sha256').equals(hash).first();
      if (cached && Date.now() - cached.created_at < UPLOAD_CACHE_TTL_MS) {
        return cached.url;
      }
    } catch {
      // Cache read is an optimisation; a broken IndexedDB must not block sending.
    }
  }

  const url = await uploadChunked(bytes, kind, sessionId, contentType);
  if (hash) {
    try {
      await db.uploadCache
        .where('created_at')
        .below(Date.now() - UPLOAD_CACHE_TTL_MS)
        .delete();
      await db.uploadCache.put({
        sha256: hash,
        url,
        session_id: sessionId,
        created_at: Date.now()
      });
    } catch {
      // Same fail-open rule as the read.
    }
  }
  return url;
}
