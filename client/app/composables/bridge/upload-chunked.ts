/**
 * Chunked media upload (ZCode parity).
 *
 * One file is uploaded sequentially: `begin` opens a session, every chunk is
 * posted in index order, and `complete` assembles it by content hash. Multiple
 * files run in parallel lanes (`MAX_CONCURRENT_FILES`), each with its own
 * sequential chunk stream. A single-chunk payload keeps the whole-body
 * `/images|/audio|/video/upload` route.
 *
 * @module bridge/upload-chunked
 */

import { uploadBytesToUrl } from './upload';

/** One chunk; mirrors the server's `HTTP_UPLOAD["chunk_size_bytes"]`. */
export const CHUNK_SIZE = 512 * 1024;

/** Parallel file lanes; mirrors `HTTP_UPLOAD["max_concurrent_files"]`. */
export const MAX_CONCURRENT_FILES = 8;

/** Called after each accepted chunk with 1-based chunk counts. */
export type UploadProgress = (sentChunks: number, totalChunks: number) => void;

/** Per-file progress for a multi-file send. */
export type FileUploadProgress = (fileIndex: number, sentChunks: number, totalChunks: number) => void;

export interface UploadFile {
  bytes: Uint8Array<ArrayBuffer>;
  kind: 'image' | 'audio' | 'video';
  contentType: string;
  fileName?: string;
}

interface RawOptions {
  method?: 'get' | 'post';
  body?: BodyInit;
  contentType?: string;
}

/**
 * One JSON request through the shared client.
 * @param url Path with query string
 * @param options Request options
 */
async function requestJson<T>(url: string, options: RawOptions = {}): Promise<T> {
  const response = await fetchApiRaw({ url, ...options });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error('server returned non-JSON response');
  }
}

/**
 * Upload one payload, chunked when it spans more than one chunk.
 *
 * @param bytes       Raw payload bytes
 * @param kind        Media kind, drives the whole-body fallback endpoint
 * @param sessionId   Session that owns the upload
 * @param contentType MIME type recorded with the chunks
 * @param onProgress  Called after every accepted chunk (1-based counts)
 * @param onBegin     Called once with the upload id and total chunk count
 * @returns           The served URL
 */
export async function uploadChunked(
  bytes: Uint8Array<ArrayBuffer>,
  kind: 'image' | 'audio' | 'video',
  sessionId: string,
  contentType: string,
  onProgress?: UploadProgress,
  onBegin?: (uploadId: string, totalChunks: number) => void
): Promise<string> {
  if (bytes.length <= CHUNK_SIZE) {
    return uploadBytesToUrl(kind, bytes, contentType);
  }

  const totalChunks = Math.ceil(bytes.length / CHUNK_SIZE);
  const beginQuery = new URLSearchParams({
    session_id: sessionId,
    filename: `${kind}-upload`,
    content_type: contentType,
    total_chunks: String(totalChunks),
    total_bytes: String(bytes.length)
  });
  const begun = await requestJson<{ ok?: boolean; upload_id?: string }>(`/upload/begin?${beginQuery.toString()}`, {
    method: 'post'
  });
  if (!begun.ok || !begun.upload_id) {
    throw new Error(`${kind} upload begin failed`);
  }
  const uploadId = begun.upload_id;
  onBegin?.(uploadId, totalChunks);

  for (let index = 0; index < totalChunks; index++) {
    const chunk = bytes.subarray(index * CHUNK_SIZE, (index + 1) * CHUNK_SIZE);
    const query = new URLSearchParams({
      upload_id: uploadId,
      chunk_index: String(index)
    });
    try {
      await requestJson(`/upload/chunk?${query.toString()}`, {
        method: 'post',
        body: chunk,
        contentType
      });
    } catch (e) {
      throw new Error(`${kind} chunk ${index} upload failed: ${e}`, { cause: e });
    }
    onProgress?.(index + 1, totalChunks);
  }

  const completed = await requestJson<{ ok?: boolean; url?: string }>(
    `/upload/complete?${new URLSearchParams({ upload_id: uploadId }).toString()}`,
    { method: 'post' }
  );
  if (!completed.ok || !completed.url) {
    throw new Error(`${kind} upload completion failed`);
  }
  return completed.url;
}

/**
 * Run at most `concurrency` tasks at a time, preserving the input order of
 * results (the caller's index is passed to `fn`).
 * @param items Inputs
 * @param concurrency Maximum in-flight tasks
 * @param fn Task
 */
export async function runPool<T>(
  items: T[],
  concurrency: number,
  fn: (item: T, index: number) => Promise<void>
): Promise<void> {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(Math.max(1, concurrency), items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor++;
      await fn(items[index] as T, index);
    }
  });
  await Promise.all(workers);
}

/**
 * Upload several files in parallel lanes; every file's chunks stay sequential.
 * @param files Files to upload
 * @param sessionId Session that owns the uploads
 * @param onFileProgress Per-file chunk progress
 * @returns URLs in the input order
 */
export async function uploadMultipleFiles(
  files: UploadFile[],
  sessionId: string,
  onFileProgress?: FileUploadProgress
): Promise<string[]> {
  const results: string[] = new Array(files.length);
  await runPool(files, MAX_CONCURRENT_FILES, async (file, index) => {
    results[index] = await uploadChunked(file.bytes, file.kind, sessionId, file.contentType, (sent, total) =>
      onFileProgress?.(index, sent, total)
    );
  });
  return results;
}

/**
 * Continue an interrupted chunked upload from the server's received count.
 * @param uploadId Session opened by `begin`
 * @param bytes Full payload (already known in the composer)
 * @param kind Media kind
 * @param contentType MIME type
 * @param onProgress Chunk progress
 * @returns The served URL
 */
export async function resumeUpload(
  uploadId: string,
  bytes: Uint8Array<ArrayBuffer>,
  kind: 'image' | 'audio' | 'video',
  contentType: string,
  onProgress?: UploadProgress
): Promise<string> {
  const status = await requestJson<{
    ok?: boolean;
    received_chunks?: number;
    total_chunks?: number;
  }>(`/upload/status?${new URLSearchParams({ upload_id: uploadId }).toString()}`);
  if (!status.ok || typeof status.received_chunks !== 'number') {
    throw new Error(`${kind} upload status failed`);
  }
  const totalChunks = Math.ceil(bytes.length / CHUNK_SIZE);
  for (let index = status.received_chunks; index < totalChunks; index++) {
    const chunk = bytes.subarray(index * CHUNK_SIZE, (index + 1) * CHUNK_SIZE);
    const query = new URLSearchParams({ upload_id: uploadId, chunk_index: String(index) });
    await requestJson(`/upload/chunk?${query.toString()}`, {
      method: 'post',
      body: chunk,
      contentType
    });
    onProgress?.(index + 1, totalChunks);
  }
  const completed = await requestJson<{ ok?: boolean; url?: string }>(
    `/upload/complete?${new URLSearchParams({ upload_id: uploadId }).toString()}`,
    { method: 'post' }
  );
  if (!completed.ok || !completed.url) {
    throw new Error(`${kind} upload completion failed`);
  }
  return completed.url;
}
