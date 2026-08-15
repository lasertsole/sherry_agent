import Dexie, { type IndexableType, type Table } from 'dexie';
import { DEFAULT_CHARACTER } from './defaultCharacter';

/** 复合索引下边界（session_id 前缀相同的最小编码值）。 */
const MIN_KEY = Dexie.minKey as IndexableType;

/** 复合索引上边界（session_id 前缀相同的最小编码值）。 */
const MAX_KEY = Dexie.maxKey as IndexableType;

/**
 * 缓存的会话历史消息记录。
 *
 * 字段与后端 `context_engine/store/db.py` 的 messages 表保持一致，
 * 以便将 `/get_history_by_turn_page` 返回的行原样缓存（去重键为 `id`）。
 */
export interface CachedMessage {
  /** 数据库自增主键（去重依据） */
  id: number;
  /** 轮次序号 */
  turn_num: number;
  /** 会话 ID */
  session_id: string;
  role: string;
  content: string | null;
  timestamp: string | null;
  /** 图片数组（与后端消息行一致，历史接口已把 JSON 解析为数组）：
   *  用户消息为 base64（无 data: 前缀），AI 消息为持久化后的绝对文件路径。 */
  images: string[] | null;
  tool_call_id: string | null;
  tool_calls: string | null;
  tool_status: string | null;
  tool_name: string | null;
  finish_reason: string | null;
  reasoning: string | null;
  reasoning_content: string | null;
}

/**
 * 缓存的角色显示信息（头像 + 名字），按 `session_id` 存储。
 *
 * 每个会话在首次打开时会对「全局待定 profile」做一次快照并锁定到该会话行，
 * 因此之后在系统配置中更新头像/名字只会影响新会话（新会话再次快照最新全局），
 * 旧会话保留各自打开时的快照不变。
 *
 * 头像可为 base64 data URL（`data:image/...;base64,...`，用户自定义上传）或
 * `/avatar/xxx.jpg` 相对 URL（内置默认，见 `defaultCharacter.ts`）；
 * 前端 `<img>` 对二者均可直接渲染，无需拼接 `static/` 静态路径。
 */
export interface CachedCharacter {
  /** 会话 ID；全局待定 profile 使用 {@link GLOBAL_SESSION_KEY} 作为主键 */
  session_id: string;
  userName: string;
  /** base64 data URL 或 `/avatar/xxx.jpg` 相对 URL */
  userAvatar: string;
  aiName: string;
  /** base64 data URL 或 `/avatar/xxx.jpg` 相对 URL */
  aiAvatar: string;
}

/** 全局待定 profile 在 character 表中的主键（非真实会话 ID）。 */
export const GLOBAL_SESSION_KEY = '__global__';

/**
 * 内置默认角色信息（便于调用方映射为 `CachedCharacter` 快照）。
 *
 * 默认头像/名字内置在前端（见 `defaultCharacter.ts`），
 * 当全局 profile 行还不存在、或某会话尚无快照时，用它作为回退值。
 */
export const DEFAULT_CACHED_CHARACTER: Pick<
  CachedCharacter,
  'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'
> = {
  userName: DEFAULT_CHARACTER.userName,
  userAvatar: DEFAULT_CHARACTER.userAvatar,
  aiName: DEFAULT_CHARACTER.aiName,
  aiAvatar: DEFAULT_CHARACTER.aiAvatar,
};

class HistoryDb extends Dexie {
  /** 会话消息缓存表 */
  messages!: Table<CachedMessage, number>;
  /** 按会话缓存的角色显示信息表（主键 session_id，含全局行 GLOBAL_SESSION_KEY） */
  character!: Table<CachedCharacter, string>;

  constructor() {
    super('ema-history-cache');
    this.version(1).stores({
      // 主键 id，复合索引 [session_id+turn_num] 用于按会话查询/去重，
      // 单列 session_id 用于清空某会话的缓存。
      messages: 'id, [session_id+turn_num], session_id',
    });
    this.version(2).stores({
      // 按 session_id 主键缓存每个会话的头像/名字快照（含全局行）。
      character: 'session_id',
    });
  }
}

/**
 * 全局唯一的 Dexie 数据库实例（用于缓存历史对话记录）。
 */
export const db = new HistoryDb();

/**
 * 将服务端返回的消息行合并写入缓存（按 `id` 去重）。
 *
 * @param rows  服务端返回的消息记录（含 `id` 字段）
 * @returns     Promise，写入完成时 resolve
 */
export async function cacheMessages(rows: CachedMessage[]): Promise<void> {
  if (!rows || rows.length === 0) return;
  await db.messages.bulkPut(rows);
}

/**
 * 读取某个会话在本地缓存中的全部消息，并按 `turn_num` 升序、`id` 升序排列。
 *
 * 通过复合索引 `[session_id+turn_num]` 做前缀查询：Dexie 会按复合键
 * `(session_id, turn_num)` 升序返回结果，从而避免在 JS 中二次排序。
 *
 * @param sessionId 会话 ID
 * @returns         缓存的该会话消息数组
 */
export async function readCachedMessages(sessionId: string): Promise<CachedMessage[]> {
  return await db.messages
    .where('[session_id+turn_num]')
    .between([sessionId, MIN_KEY], [sessionId, MAX_KEY])
    .toArray();
}

/**
 * 计算某个会话在本地缓存中的最大轮次。
 *
 * 复合索引 `[session_id+turn_num]` 按 `turn_num` 升序返回该会话的行，
 * 因此最大 `turn_num` 可取最后一条记录的值，避免在 JS 中遍历全部行。
 *
 * 若缓存为空则返回 `0`（后端要求 `min_turn_num >= 1`，
 * 客户端不传该值上限，交由服务端逻辑处理）。
 *
 * @param sessionId 会话 ID
 * @returns         缓存中的最大 turn_num（无缓存时为 0）
 */
export async function cachedMaxTurnNum(sessionId: string): Promise<number> {
  const last = await db.messages
    .where('[session_id+turn_num]')
    .between([sessionId, MIN_KEY], [sessionId, MAX_KEY])
    .last();
  return last ? last.turn_num : 0;
}

/**
 * 清除某个会话在本地缓存中的全部消息。
 *
 * @param sessionId 会话 ID
 */
export async function clearCachedSession(sessionId: string): Promise<void> {
  await db.messages.where('session_id').equals(sessionId).delete();
}

/**
 * 写入（缓存 / 覆盖）某个会话的角色显示信息快照。
 *
 * @param char 包含 `session_id`（真实会话 ID 或 `GLOBAL_SESSION_KEY`）的角色信息
 */
export async function cacheCharacter(char: CachedCharacter): Promise<void> {
  await db.character.put(char);
}

/**
 * 读取某个会话的角色显示信息快照（无记录时返回 `undefined`）。
 *
 * @param sessionId 会话 ID 或 `GLOBAL_SESSION_KEY`
 */
export async function readCachedCharacter(sessionId: string): Promise<CachedCharacter | undefined> {
  return await db.character.get(sessionId);
}

/**
 * 清除某个会话的角色显示信息快照（删除会话时同步清理）。
 *
 * @param sessionId 会话 ID（真实会话，不应传 `GLOBAL_SESSION_KEY`）
 */
export async function clearCachedCharacter(sessionId: string): Promise<void> {
  await db.character.delete(sessionId);
}
