import { describe, it, expect } from 'vitest';
import { ref } from 'vue';
import { useSessionLifecycle } from '../use-session-lifecycle';
import { DEFAULT_CACHED_CHARACTER } from '../db';

/**
 * The character snapshot mapping: a preset that names nobody (编程助手) must stay
 * nameless — substituting the built-in persona here showed 橘雪莉 / 远野汉娜 with
 * their photos in a session the user had configured otherwise.
 */
describe('use-session-lifecycle character mapping', () => {
  it('keeps an explicitly empty name and avatar instead of the built-in persona', () => {
    const { applyCharacterSnapshot, characterInfo } = useSessionLifecycle(ref([]));

    applyCharacterSnapshot({ userName: '', userAvatar: '', aiName: '', aiAvatar: '' });

    expect(characterInfo.value).toEqual({ userName: '', userAvatar: '', aiName: '', aiAvatar: '' });
  });

  it('falls back to the built-in character only for MISSING fields', () => {
    const { applyCharacterSnapshot, characterInfo } = useSessionLifecycle(ref([]));

    applyCharacterSnapshot({ userName: '小明' } as never);

    expect(characterInfo.value.userName).toBe('小明');
    expect(characterInfo.value.aiName).toBe(DEFAULT_CACHED_CHARACTER.aiName);
    expect(characterInfo.value.aiAvatar).toBe(DEFAULT_CACHED_CHARACTER.aiAvatar);
  });

  it('uses the built-in character when the session has no snapshot at all', () => {
    const { applyCharacterSnapshot, characterInfo } = useSessionLifecycle(ref([]));

    applyCharacterSnapshot(undefined);

    expect(characterInfo.value).toEqual(DEFAULT_CACHED_CHARACTER);
  });
});
