import { describe, it, expect } from 'vitest';
import {
  DEFAULT_USER_AVATAR,
  DEFAULT_AI_AVATAR,
  DEFAULT_USER_NAME,
  DEFAULT_AI_NAME,
  DEFAULT_CHARACTER
} from '../defaultCharacter';

describe('defaultCharacter constants', () => {
  it('exposes the builtin avatar URLs served from the frontend public/ dir', () => {
    expect(DEFAULT_USER_AVATAR).toBe('/avatar/user.jpg');
    expect(DEFAULT_AI_AVATAR).toBe('/avatar/assistant.jpg');
  });

  it('carries the default display names', () => {
    expect(DEFAULT_USER_NAME).toBe('远野汉娜');
    expect(DEFAULT_AI_NAME).toBe('橘雪莉');
  });

  it('assembles the full DEFAULT_CHARACTER snapshot with matching fields', () => {
    expect(DEFAULT_CHARACTER).toEqual({
      userName: DEFAULT_USER_NAME,
      userAvatar: DEFAULT_USER_AVATAR,
      aiName: DEFAULT_AI_NAME,
      aiAvatar: DEFAULT_AI_AVATAR
    });
  });
});
