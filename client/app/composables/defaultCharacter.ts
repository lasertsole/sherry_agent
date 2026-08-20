/**
 * 内置默认角色显示信息（头像 + 名字）。
 *
 * 默认头像/名字与改造前（character.json）保持一致，但全部内置在前端，不使用后端请求：
 * - 用户默认：远野汉娜 + `/avatar/user.jpg`
 * - 助手默认：橘雪莉 + `/avatar/assistant.jpg`
 *
 * 默认头像图片已随前端静态资源打包（`client/public/avatar/`），故默认快照的 avatar
 * 存为相对 URL（`/avatar/xxx.jpg`）。`<img :src>` 对相对 URL 与 base64 data URL 均原生兼容，
 * 因此渲染层无需区分两种形态。
 */

/** 内置默认角色信息结构（与 `CachedCharacter` 去掉 `session_id` 一致）。 */
export interface DefaultCharacterInfo {
  userName: string;
  /** base64 data URL 或 `/avatar/xxx.jpg` 相对 URL（默认） */
  userAvatar: string;
  aiName: string;
  /** base64 data URL 或 `/avatar/xxx.jpg` 相对 URL（默认） */
  aiAvatar: string;
}

/** 用户默认头像相对 URL（对应 `client/public/avatar/user.jpg`）。 */
export const DEFAULT_USER_AVATAR = '/avatar/user.jpg';

/** 助手默认头像相对 URL（对应 `client/public/avatar/assistant.jpg`）。 */
export const DEFAULT_AI_AVATAR = '/avatar/assistant.jpg';

/** 用户默认名字。 */
export const DEFAULT_USER_NAME = '远野汉娜';

/** 助手默认名字。 */
export const DEFAULT_AI_NAME = '橘雪莉';

/** 内置默认角色信息。 */
export const DEFAULT_CHARACTER: DefaultCharacterInfo = {
  userName: DEFAULT_USER_NAME,
  userAvatar: DEFAULT_USER_AVATAR,
  aiName: DEFAULT_AI_NAME,
  aiAvatar: DEFAULT_AI_AVATAR,
};
