/**
 * Media resolution for chat messages (ChatBox).
 *
 * Extracted from `ChatBox.vue` with identical URL semantics (see the
 * `MessageItem.images/audios/videos` comments in `pages/home/type.ts`):
 *  - User messages: raw base64 (without a `data:` prefix) → assembled locally
 *    into `data:<mime>;base64,<data>`;
 *  - AI messages: persisted absolute file paths → served via the backend
 *    `/media` endpoint (basename only, via `mediaUrl`);
 *  - Absolute `http(s)://` URLs pass through verbatim (never misrouted by
 *    extension into a `/media` request).
 *
 * The per-message image error tracking (`failedImageSources`) also lives here so
 * every rendered message shares ONE failed-source set per ChatBox instance,
 * exactly as before.
 *
 * @module composables/use-chat-media
 */
import type { MessageItem } from '~/pages/home/type';

/**
 * Resolve the image entries in a message into renderable <img src> values.
 * Semantics (see the MessageItem.images comment in type.ts):
 *  - User messages: raw base64 (without the data: prefix) → assembled locally into
 *    data:image/*;base64,<data>
 *  - AI messages: persisted absolute file paths → served via the backend /media endpoint, which
 *    returns the image by session_id + filename; the original basename (e.g. <ts>.png) must be
 *    taken, dropping the directory part of the file path.
 * Decision basis: a file path necessarily contains a backslash \ or ends with a common media
 * extension; the base64 alphabet happens to contain / and + (and is usually padded with =),
 * so "/" must never be used as the "file path" test —— that would misjudge the user's raw
 * base64 image as a /media request (the root cause of 4 historical "media not found" bugs).
 * @param message
 * @param entry
 */
export function resolveImageSrc(message: MessageItem, entry: string): string {
  const s = (entry ?? '').trim();
  if (!s) return '';
  // Absolute URLs (http/https) pass through as-is: user images injected by the middleware are
  // already-served http(s)://…/images/<hash>.png addresses, which must be rendered verbatim;
  // do not misjudge them as /media file paths by extension (otherwise 404 / broken image).
  if (/^https?:\/\//i.test(s)) return s;
  const isFilePath = s.includes('\\') || /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(s);
  if (isFilePath) {
    // AI messages: fetch via /media
    return mediaUrl(message.session_id, s);
  }
  // User messages: local base64
  return `data:image/*;base64,${s}`;
}

/**
 * Extract image URLs injected by the multimodal processor into message content.
 *
 * The middleware persists a marker like:
 *   "[System: The user uploaded N image(s). Location: http://…/images/<hash>.png,…]"
 * This fallback resolves those served URLs so history/legacy rows whose
 * `images` field is empty still render their images.
 * @param content
 */
export function extractContentImageUrls(content: string): string[] {
  if (!content) return [];
  const m = content.match(/Location:\s*([^\]\n]+)/);
  if (!m) return [];
  return (m[1] ?? '')
    .split(/[,\s]+/)
    .map(u => u.replace(/[\]\s.,!;:]+$/g, ''))
    .filter(u => /^https?:\/\//i.test(u));
}

/**
 * Images to render for a message: explicit `images` wins; otherwise fall back
 * to URLs parsed from the content's Location marker.
 * @param message
 */
export function messageImages(message: MessageItem): string[] {
  const explicit = message.images ?? [];
  return explicit.length > 0 ? explicit : extractContentImageUrls(message.content);
}

/**
 * Resolve the audio/video entries in a message into playable src values.
 * Semantics are identical to `resolveImageSrc` (user messages → local base64; AI messages →
 * /media file paths):
 *  - User messages: raw base64 (without the data: prefix) → assembled locally into
 *    data:audio/*;base64,<data> or data:video/*;base64,<data>
 *  - AI messages: persisted absolute file paths → fetched via the backend /media endpoint, with
 *    the basename used to build the URL
 *  - Absolute http(s):// URLs pass through as-is
 * @param message
 * @param entry
 * @param mimePrefix
 */
export function resolveMediaSrc(message: MessageItem, entry: string, mimePrefix: string): string {
  const s = (entry ?? '').trim();
  if (!s) return '';
  // Absolute URLs (http/https) pass through as-is: media addresses injected by the middleware start with http(s)://
  if (/^https?:\/\//i.test(s)) return s;
  const isFilePath = s.includes('\\') || /\.(mp3|wav|ogg|m4a|aac|flac|mp4|webm|mov|avi|mkv|m4v)$/i.test(s);
  if (isFilePath) {
    // AI messages: fetch via /media
    return mediaUrl(message.session_id, s);
  }
  // User messages: local base64 (data:<mimePrefix>;base64,<data>)
  return `data:${mimePrefix};base64,${s}`;
}

/**
 * Resolve the audio src (mime prefix audio/*)
 * @param message
 * @param entry
 */
export const resolveAudioSrc = (message: MessageItem, entry: string): string =>
  resolveMediaSrc(message, entry, 'audio/*');

/**
 * Resolve the video src (mime prefix video/*)
 * @param message
 * @param entry
 */
export const resolveVideoSrc = (message: MessageItem, entry: string): string =>
  resolveMediaSrc(message, entry, 'video/*');

/**
 * Audio entries carried by this message
 * @param message
 */
export const messageAudios = (message: MessageItem): string[] => message.audios ?? [];

/**
 * Video entries carried by this message
 * @param message
 */
export const messageVideos = (message: MessageItem): string[] => message.videos ?? [];

/**
 * Create the per-ChatBox image-failure tracker.
 *
 * An image src that fails to load (e.g. the /media file referenced by a historical
 * message no longer exists on disk → 404) is recorded once; later re-renders no
 * longer attempt to load that src and directly show the placeholder block instead.
 * The set is shared by every message of the one ChatBox instance (rendered by the
 * attachments child components), matching the previous behavior.
 */
export function useChatMedia() {
  const failedImageSources = reactive(new Set<string>());

  /**
   * Callback for <img> load failures (including 404/network errors): record the failed src in the set to hide the broken image.
   * @param _event
   * @param src
   */
  const onImageError = (_event: Event, src: string) => {
    if (src) {
      failedImageSources.add(src);
    }
  };

  return { failedImageSources, onImageError };
}
