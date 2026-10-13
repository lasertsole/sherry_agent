/**
 * Base64 media upload for the browser chat transport.
 *
 * @module bridge/upload
 */

/** Media kind — drives the upload endpoint path and the default MIME/content-type. */
export type UploadMediaKind = 'image' | 'audio' | 'video';

/** Per-kind upload endpoint suffix (POST `${baseURL}/${endpoint}`). */
const KIND_ENDPOINT: Record<UploadMediaKind, string> = {
  image: '/images/upload',
  audio: '/audio/upload',
  video: '/video/upload'
};

/** Wildcard MIME (e.g. `image/`, `audio/`, `video/`) to parse a `data:...;base64,` prefix. */
const KIND_MIME_PREFIX: Record<UploadMediaKind, string> = {
  image: 'image/',
  audio: 'audio/',
  video: 'video/'
};

/** Fallback content-type when the payload carries no `data:` prefix. */
const KIND_DEFAULT_CONTENT_TYPE: Record<UploadMediaKind, string> = {
  image: 'image/png',
  audio: 'audio/webm',
  video: 'video/mp4'
};

/** Human-readable kind label used in error messages. */
export const KIND_LABEL: Record<UploadMediaKind, string> = {
  image: 'Image',
  audio: 'Audio',
  video: 'Video'
};

/**
 * Upload base64 media (image/audio/video) to the backend's corresponding
 * `/images|/audio|/video/upload` endpoint and return the URL list.
 *
 * The request goes through the shared API client (`fetchApiRaw`): same base URL
 * and token policy as the JSON calls, but intentionally raw — this function owns
 * the failure contract (HTTP status / non-JSON body inspection) and throws
 * labelled errors, so no retry and no user-visible toast may be added here.
 *
 * @param kind       Media kind (image | audio | video); determines the upload endpoint and MIME parsing rules
 * @param base64List List of base64-encoded strings (may carry a data:<mime>;base64, prefix)
 * @returns          Array of uploaded URLs (same order as the input)
 */
export async function uploadBase64ToUrls(kind: UploadMediaKind, base64List: string[]): Promise<string[]> {
  const label = KIND_LABEL[kind];
  const urls: string[] = [];
  for (const base64 of base64List) {
    let contentType = KIND_DEFAULT_CONTENT_TYPE[kind];
    let pureBase64 = base64;

    // If a data:<mime>;base64, prefix is present, strip it and extract the MIME type
    const match = pureBase64.match(new RegExp(`^data:(${KIND_MIME_PREFIX[kind]}[w.+-]+);base64,(.+)$`));
    if (match) {
      contentType = match[1] ?? contentType;
      pureBase64 = match[2] ?? pureBase64;
    }

    const bytes = Uint8Array.from(atob(pureBase64), c => c.charCodeAt(0));

    let resp: Response;
    try {
      resp = await fetchApiRaw({
        url: KIND_ENDPOINT[kind],
        method: 'post',
        body: bytes,
        contentType
      });
    } catch (e) {
      throw new Error(`${label} upload network error: ${e}`, { cause: e });
    }

    if (!resp.ok) {
      throw new Error(`${label} upload failed: HTTP ${resp.status}`);
    }

    let json: { success?: boolean; url?: string; filename?: string };
    try {
      json = await resp.json();
    } catch {
      throw new Error(`${label} upload failed: server returned non-JSON response`);
    }

    if (!json.success || !json.url) {
      throw new Error(`${label} upload failed: ${JSON.stringify(json)}`);
    }

    urls.push(json.url);
  }
  return urls;
}
