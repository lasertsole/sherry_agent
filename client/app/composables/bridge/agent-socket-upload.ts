/**
 * Outbound media upload + payload serialization for the agent socket.
 *
 * Base64 media is uploaded to the backend before the chat frame is sent; the
 * frame then references the returned URLs. Error wrapping (per-kind label,
 * `cause` chained) is unchanged from the previous in-class implementation.
 *
 * @module bridge/agent-socket-upload
 */
import type { ChatRequest } from './chat-types';
import { KIND_LABEL, uploadBase64ToUrls, type UploadMediaKind } from './upload';
import { API_BASE_URL } from '../env';

/** A resolved media-URL set attached to one outgoing payload. */
export interface MediaUrls {
  images: string[];
  audios: string[];
  videos: string[];
}

/**
 * Whether the request carries any base64 media that must be uploaded first.
 * @param request
 */
export function requestHasMedia(request: ChatRequest): boolean {
  return (
    (request.image_base64_list?.length ?? 0) > 0 ||
    (request.audio_bytes_list?.length ?? 0) > 0 ||
    (request.video_bytes_list?.length ?? 0) > 0
  );
}

/**
 * Upload every media kind of a request and return the URL lists.
 * @param request
 */
export async function uploadRequestMedia(request: ChatRequest): Promise<MediaUrls> {
  const upload = async (kind: UploadMediaKind, list?: string[]): Promise<string[]> => {
    if (!list || list.length === 0) return [];
    try {
      return await uploadBase64ToUrls(kind, list, API_BASE_URL);
    } catch (e) {
      throw new Error(`${KIND_LABEL[kind]} upload failed: ${e}`, { cause: e });
    }
  };
  return {
    images: await upload('image', request.image_base64_list),
    audios: await upload('audio', request.audio_bytes_list),
    videos: await upload('video', request.video_bytes_list)
  };
}

/**
 * Serialize the outgoing agent frame (protocol field order preserved).
 * @param sessionId
 * @param msgId
 * @param request
 * @param urls
 */
export function buildAgentPayload(sessionId: string, msgId: string, request: ChatRequest, urls: MediaUrls): string {
  return JSON.stringify({
    session_id: sessionId,
    msg_id: msgId,
    origin: request.origin || 'user',
    multi_modal_message: {
      text: request.text || '',
      image_base64_list: [],
      image_path_list: urls.images,
      audio_bytes_list: [],
      audio_path_list: urls.audios,
      video_bytes_list: [],
      video_path_list: urls.videos
    }
  });
}
