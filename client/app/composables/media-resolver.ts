/** Backend HTTP base URL with trailing slashes stripped (the `/media` endpoint root). */
const MEDIA_BASE_URL = API_BASE_URL.replace(/\/+$/, '');

/**
 * Build the backend `/media` URL for a persisted media file.
 *
 * Accepts a bare filename or a full persisted file path: the file may carry any
 * directory prefix, so only its basename is sent as the `filename` query param.
 * @param sessionId Session the media file belongs to
 * @param filePathOrName Persisted absolute file path (or bare filename)
 */
export function mediaUrl(sessionId: string | null | undefined, filePathOrName: string): string {
  const filename = filePathOrName.split(/[\\/]/).pop() || '';
  return `${MEDIA_BASE_URL}/media?session_id=${encodeURIComponent(sessionId ?? '')}&filename=${encodeURIComponent(filename)}`;
}

/**
 * Strip the `data:<mime>;base64,` prefix from a data URL, keeping only the base64 payload.
 * @param dataUrl
 */
export function extractBase64(dataUrl: string): string {
  return dataUrl.split(',')[1] ?? '';
}
