/**
 * Short-TTL upload dedupe with resumable state.
 *
 * The payload is hashed with SHA-256 and looked up in the Dexie `uploadCache`
 * table: the same bytes sent twice inside the TTL reuse the first URL instead of
 * re-uploading. While a chunked upload runs, its `upload_id` and chunk count are
 * persisted too, so a refresh can resume through `/upload/status`; the cache is
 * best-effort — blocked IndexedDB or a browser without `crypto.subtle` simply
 * uploads.
 *
 * @module bridge/upload-cache
 */
import { db, type UploadCacheRow } from '../db';
import type { UploadMediaKind } from './upload';
import {
  CHUNK_SIZE,
  MAX_CONCURRENT_FILES,
  runPool,
  uploadChunked,
  type FileUploadProgress,
  type UploadFile,
  type UploadProgress
} from './upload-chunked';

/** Mirrors the server's `HTTP_UPLOAD["read_cache_ttl_ms"]`. */
export const UPLOAD_CACHE_TTL_MS = 30_000;

/**
 * SHA-256 of the payload as lowercase hex, or `null` when the platform cannot
 * hash it.
 * @param bytes Payload bytes
 */
async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string | null> {
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
 * @param onProgress  Chunk progress
 * @param fileName    Optional name persisted with the resumable row
 * @returns           The served URL
 */
export async function uploadWithCache(
  bytes: Uint8Array<ArrayBuffer>,
  kind: UploadMediaKind,
  sessionId: string,
  contentType: string,
  onProgress?: UploadProgress,
  fileName?: string
): Promise<string> {
  const hash = await sha256Hex(bytes);
  let cachedRow: UploadCacheRow | undefined;
  if (hash) {
    try {
      cachedRow = await db.uploadCache.where('sha256').equals(hash).first();
      const completed = !cachedRow?.status || cachedRow.status === 'completed';
      if (cachedRow && completed && cachedRow.url && Date.now() - cachedRow.created_at < UPLOAD_CACHE_TTL_MS) {
        return cachedRow.url;
      }
    } catch {
      // Cache read is an optimisation; a broken IndexedDB must not block sending.
    }
  }

  const totalChunks = Math.ceil(bytes.length / CHUNK_SIZE) || 1;
  let rowId: number | undefined;
  if (hash) {
    try {
      if (cachedRow?.id !== undefined) {
        rowId = cachedRow.id;
        await db.uploadCache.update(rowId, {
          url: '',
          session_id: sessionId,
          created_at: Date.now(),
          file_name: fileName,
          total_chunks: totalChunks,
          status: 'uploading'
        });
      } else {
        rowId = await db.uploadCache.add({
          sha256: hash,
          url: '',
          session_id: sessionId,
          created_at: Date.now(),
          file_name: fileName,
          total_chunks: totalChunks,
          status: 'uploading'
        });
      }
    } catch {
      // Same fail-open rule as the read.
    }
  }

  let url: string;
  try {
    url = await uploadChunked(bytes, kind, sessionId, contentType, onProgress, async (uploadId, chunks) => {
      if (rowId === undefined) return;
      try {
        await db.uploadCache.update(rowId, { upload_id: uploadId, total_chunks: chunks });
      } catch {
        // The upload itself must not fail because of the journal.
      }
    });
  } catch (error) {
    if (rowId !== undefined) {
      try {
        await db.uploadCache.update(rowId, { status: 'failed' });
      } catch {
        // fail-open
      }
    }
    throw error;
  }

  if (rowId !== undefined && hash) {
    try {
      await db.uploadCache.update(rowId, {
        url,
        status: 'completed',
        created_at: Date.now()
      });
      await db.uploadCache
        .where('created_at')
        .below(Date.now() - UPLOAD_CACHE_TTL_MS)
        .delete();
    } catch {
      // fail-open
    }
  }
  return url;
}

/**
 * Upload files in parallel lanes, each through the content-hash cache.
 * @param files Files to upload
 * @param sessionId Session that owns the uploads
 * @param onFileProgress Per-file chunk progress
 * @returns URLs in the input order
 */
export async function uploadMultipleWithCache(
  files: UploadFile[],
  sessionId: string,
  onFileProgress?: FileUploadProgress
): Promise<string[]> {
  const results: string[] = new Array(files.length);
  await runPool(files, MAX_CONCURRENT_FILES, async (file, index) => {
    results[index] = await uploadWithCache(
      file.bytes,
      file.kind,
      sessionId,
      file.contentType,
      (sent, total) => onFileProgress?.(index, sent, total),
      file.fileName
    );
  });
  return results;
}
