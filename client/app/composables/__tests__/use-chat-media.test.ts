import { describe, it, expect } from 'vitest';
import {
  resolveImageSrc,
  resolveMediaSrc,
  resolveAudioSrc,
  resolveVideoSrc,
  extractContentImageUrls,
  messageImages,
  messageAudios,
  messageVideos,
  useChatMedia
} from '../use-chat-media';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.AI,
  content: '',
  id: 1,
  turn_num: 0,
  timestamp: 't',
  ...over
});

describe('resolveImageSrc', () => {
  it('returns empty for empty/whitespace entries', () => {
    expect(resolveImageSrc(msg({}), '')).toBe('');
    expect(resolveImageSrc(msg({}), '   ')).toBe('');
  });

  it('passes absolute http(s) URLs through unchanged (no /media misroute)', () => {
    const url = 'http://127.0.0.1:8080/images/abc123.png';
    expect(resolveImageSrc(msg({}), url)).toBe(url);
    expect(resolveImageSrc(msg({}), 'https://example.com/a.PNG')).toBe('https://example.com/a.PNG');
  });

  it('routes file paths (backslash or media extension) through the /media endpoint', () => {
    expect(resolveImageSrc(msg({ session_id: 's1' }), 'C:/x/media/123.png')).toBe(
      'http://localhost:8080/media?session_id=s1&filename=123.png'
    );
    expect(resolveImageSrc(msg({ session_id: 's1' }), 'C:\\x\\media\\123.jpg')).toBe(
      'http://localhost:8080/media?session_id=s1&filename=123.jpg'
    );
    expect(resolveImageSrc(msg({ session_id: 's1' }), 'a/b/c.WEBP')).toBe(
      'http://localhost:8080/media?session_id=s1&filename=c.WEBP'
    );
  });

  it('treats raw base64 (incl. / + and =) as a local data URL, never a file path', () => {
    const b64 = 'ab/c+d==';
    expect(resolveImageSrc(msg({}), b64)).toBe(`data:image/*;base64,${b64}`);
  });
});

describe('resolveMediaSrc / audio / video', () => {
  it('builds a data URL for raw base64 with the requested mime prefix', () => {
    expect(resolveAudioSrc(msg({}), 'QUJD')).toBe('data:audio/*;base64,QUJD');
    expect(resolveVideoSrc(msg({}), 'RUZH')).toBe('data:video/*;base64,RUZH');
  });

  it('routes media file paths through /media and passes absolute URLs through', () => {
    expect(resolveAudioSrc(msg({ session_id: 's' }), 'C:/m/a.mp3')).toBe(
      'http://localhost:8080/media?session_id=s&filename=a.mp3'
    );
    expect(resolveVideoSrc(msg({ session_id: 's' }), 'C:/m/a.mkv')).toBe(
      'http://localhost:8080/media?session_id=s&filename=a.mkv'
    );
    expect(resolveMediaSrc(msg({}), 'https://x/y.mp4', 'video/*')).toBe('https://x/y.mp4');
    expect(resolveMediaSrc(msg({}), '', 'audio/*')).toBe('');
  });
});

describe('extractContentImageUrls', () => {
  it('parses http(s) URLs from the Location marker, stripping trailing punctuation', () => {
    const content =
      '[System: The user uploaded 2 image(s). Location: http://127.0.0.1:8080/images/a.png, https://x/b.jpeg. But...]';
    expect(extractContentImageUrls(content)).toEqual(['http://127.0.0.1:8080/images/a.png', 'https://x/b.jpeg']);
  });

  it('returns [] without a Location marker or with no http URLs', () => {
    expect(extractContentImageUrls('plain text')).toEqual([]);
    expect(extractContentImageUrls('')).toEqual([]);
    expect(extractContentImageUrls('Location: not-a-url')).toEqual([]);
  });
});

describe('messageImages / messageAudios / messageVideos', () => {
  it('prefers explicit images and falls back to the content Location marker', () => {
    expect(messageImages(msg({ images: ['a'] }))).toEqual(['a']);
    const content = 'Location: http://x/i.png.';
    expect(messageImages(msg({ images: [], content }))).toEqual(['http://x/i.png']);
    expect(messageImages(msg({ content: 'no marker' }))).toEqual([]);
  });

  it('returns [] when audio/video fields are absent', () => {
    expect(messageAudios(msg({}))).toEqual([]);
    expect(messageVideos(msg({}))).toEqual([]);
    expect(messageAudios(msg({ audios: ['a'] }))).toEqual(['a']);
    expect(messageVideos(msg({ videos: ['v'] }))).toEqual(['v']);
  });
});

describe('useChatMedia', () => {
  it('shares one failed-source set and records only non-empty srcs', () => {
    const { failedImageSources, onImageError } = useChatMedia();
    expect(failedImageSources.size).toBe(0);
    onImageError(new Event('error'), '');
    expect(failedImageSources.size).toBe(0);
    onImageError(new Event('error'), 'http://x/broken.png');
    expect([...failedImageSources]).toEqual(['http://x/broken.png']);
    onImageError(new Event('error'), 'http://x/broken.png');
    expect(failedImageSources.size).toBe(1);
  });
});
