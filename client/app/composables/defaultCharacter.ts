/**
 * Built-in default character display info (avatar + name).
 *
 * The default avatar/name match the pre-refactor values (character.json), but are now
 * fully built into the frontend without backend requests:
 * - User default: Tono Hannah (the string literal keeps the original name) + `/avatar/user.jpg`
 * - Assistant default: Tachibana Sherry (the string literal keeps the original name) + `/avatar/assistant.jpg`
 *
 * The default avatar images ship with the frontend static assets
 * (`client/public/avatar/`), so the default snapshots store the avatar as a relative
 * URL (`/avatar/xxx.jpg`). `<img :src>` natively supports both relative URLs and
 * base64 data URLs, so the render layer does not need to distinguish the two forms.
 */

/** Structure of the built-in default character info (same as `CachedCharacter` minus `session_id`). */
export interface DefaultCharacterInfo {
  userName: string;
  /** base64 data URL or the `/avatar/xxx.jpg` relative URL (default) */
  userAvatar: string;
  aiName: string;
  /** base64 data URL or the `/avatar/xxx.jpg` relative URL (default) */
  aiAvatar: string;
}

/** User default avatar relative URL (corresponds to `client/public/avatar/user.jpg`). */
export const DEFAULT_USER_AVATAR = '/avatar/user.jpg';

/** Assistant default avatar relative URL (corresponds to `client/public/avatar/assistant.jpg`). */
export const DEFAULT_AI_AVATAR = '/avatar/assistant.jpg';

/** User default name. */
export const DEFAULT_USER_NAME = '远野汉娜';

/** Assistant default name. */
export const DEFAULT_AI_NAME = '橘雪莉';

/** Built-in default character info. */
export const DEFAULT_CHARACTER: DefaultCharacterInfo = {
  userName: DEFAULT_USER_NAME,
  userAvatar: DEFAULT_USER_AVATAR,
  aiName: DEFAULT_AI_NAME,
  aiAvatar: DEFAULT_AI_AVATAR
};
