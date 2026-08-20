import Dexie, { type IndexableType, type Table } from 'dexie';
import { DEFAULT_CHARACTER } from './defaultCharacter';
import type { MessageItem } from '@/pages/home/type';

/** 复合索引下边界（session_id 前缀相同的最小编码值）�?*/
const MIN_KEY = Dexie.minKey as IndexableType;

/** 复合索引上边界（session_id 前缀相同的最小编码值）�?*/
const MAX_KEY = Dexie.maxKey as IndexableType;

/**
 * 缓存的会话历史消息记录�?
 *
 * 字段与后�?`context_engine/store/db.py` �?messages 表保持一致，
 * 以便�?`/get_history_by_turn_page` 返回的行原样缓存（去重键�?`id`）�?
 */
export interface CachedMessage {
  /** 数据库自增主键（去重依据�?*/
  id: number;
  /** 轮次序号 */
  turn_num: number;
  /** 会话 ID */
  session_id: string;
  role: string;
  content: string | null;
  timestamp: string | null;
  /** 图片数组（与后端消息行一致，历史接口已把 JSON 解析为数组）�?
   *  用户消息�?base64（无 data: 前缀），AI 消息为持久化后的绝对文件路径�?*/
  images: string[] | null;
  audios: string[] | null;
  videos: string[] | null;
  tool_call_id: string | null;
  tool_calls: string | null;
  tool_status: string | null;
  tool_name: string | null;
  finish_reason: string | null;
  reasoning: string | null;
  reasoning_content: string | null;
  /** 模型名称（来自后端历史行的 model_name） */
  model_name: string | null;
  /** 输入 token 数（来自后端历史行的 input_tokens） */
  input_tokens: number | null;
  /** 输出 token 数（来自后端历史行的 output_tokens） */
  output_tokens: number | null;
}

/**
 * 缓存的角色显示信息（头像 + 名字），�?`session_id` 存储�?
 *
 * 每个会话在首次打开时会对「全局待定 profile」做一次快照并锁定到该会话行，
 * 因此之后在系统配置中更新头像/名字只会影响新会话（新会话再次快照最新全局），
 * 旧会话保留各自打开时的快照不变�?
 *
 * 头像可为 base64 data URL（`data:image/...;base64,...`，用户自定义上传）或
 * `/avatar/xxx.jpg` 相对 URL（内置默认，�?`defaultCharacter.ts`）；
 * 前端 `<img>` 对二者均可直接渲染，无需拼接 `static/` 静态路径�?
 */
export interface CachedCharacter {
  /** 会话 ID；全局待定 profile 使用 {@link GLOBAL_SESSION_KEY} 作为主键 */
  session_id: string;
  userName: string;
  /** base64 data URL �?`/avatar/xxx.jpg` 相对 URL */
  userAvatar: string;
  aiName: string;
  /** base64 data URL �?`/avatar/xxx.jpg` 相对 URL */
  aiAvatar: string;
}

/**
 * 缓存的会话列表条目（本地持久化的空会话占位）�?
 *
 * 后端会话列表由消息表派生，创建空会话（尚未发消息）时服务端不存在对应记录�?
 * 为满足「新建对话后刷新仍保留」的离线场景，前端在 IndexedDB 中持久化这些占位条目�?
 * �?`historyList` 的内存态对应，可在刷新 / 重启后恢复�?
 */
export interface CachedSessionMeta {
  /** 会话 ID */
  id: string;
  /** 会话标题（新建未命名会话的占位标题） */
  title: string;
  /** 创建时间（本地时间字符串，用于左侧列表展示） */
  createTime: string;
  /** 本地排序时间戳（用于按最新优先合并排序） */
  updatedAt: number;
}

/**
 * 用户上传的聊天区背景图（浅色/深色主题下均显示）�?
 *
 * 全局唯一行（主键 {@link GLOBAL_SESSION_KEY}），存一份 base64 data URL，
 * 以及主题化遮罩透明度；不随会话区分�?从系统配置「背景设置」读写。
 */
export interface CachedBackground {
  /** 全局唯一主键，固定为 {@link GLOBAL_SESSION_KEY} */
  session_id: string;
  /** 背景图 base64 data URL（`data:image/...;base64,...`），空字符串表示尚未设置 */
  backgroundUrl: string;
  /** 遮罩透明度（0-100 整数）。浅色主题下为白色遮罩、深色主题下为黑色遮罩，
   *  数值越大照片越接近纯白/纯黑直至完全淹没。默认 0 表示不叠加遮罩。 */
  backgroundOpacity: number;
}

/**
 * 进行中（in-flight）一整个 agent 轮次的草稿缓存记录�?
 *
 * 后端 `aafter_agent` 仅在整个 LangGraph 节点成功返回时每轮触发一次持久化�?
 * 工具调用阶段、以及中途异�?中止的轮次在服务�?*完全不会落库**。为避免
 * 「agent 回复『寒暄→分析→调用工具→观察工具结果→最终结果』时，因某一�?
 * 报错或最终结果未输出而丢失前面已产生的阶段内容」，前端把进行中的整一�?
 * `MessageItem[]` 缓存到本�?IndexedDB �?`drafts` 表�?
 *
 * 键为 `[session_id + turn_num]`：`turn_num` 与实时消息使用相同的**正轮次号**
 * （见 {@link DraftTurn.turn_num}）。服务端 `aafter_agent` 只在整个 LangGraph 节点
 * 成功返回后才落库该轮，in-flight / error / abort 轮在服务端不产生任何行，因此草稿
 * �?`turn_num` 不会与任何已落库行冲突；草稿独立存于 `drafts` 表，也不污染
 * `cachedMaxTurnNum`（该函数只查 `messages` 表）�?
 *
 * 每执行一步（send / tool_start / tool_end / tool_result / error / 首个文本 /
 * 服务�?onDone）就整体重写本行，做到「每步缓存」；文本追加�?200ms 去抖合并�?
 * 服务端成功落库（onDone �?`loadSessionHistory` 对账）后清除本行�?
 */
export interface DraftTurn {
  /** 会话 ID */
  session_id: string;
  /**
   * 本轮次号，与服务�?`messages` 表同一会话内递增的正 `turn_num` 一致�?
   * 草稿行的 `messages` 内各�?`MessageItem` 使用本地负临�?id（`tempIdCounter`
   * �?-1000000 递增，同轮内 id 升序即创建顺序），对账时可将草稿�?id 行替换为
   * 服务端正 id 行完成去重�?
   */
  turn_num: number;
  /** 进行中的整一轮消息（本地负临�?id，创建顺序即 id 升序�?*/
  messages: MessageItem[];
}

/** 全局待定 profile �?character 表中的主键（非真实会�?ID）�?*/
export const GLOBAL_SESSION_KEY = '__global__';

/**
 * 内置默认角色信息（便于调用方映射�?`CachedCharacter` 快照）�?
 *
 * 默认头像/名字内置在前端（�?`defaultCharacter.ts`），
 * 当全局 profile 行还不存在、或某会话尚无快照时，用它作为回退值�?
 */
export const DEFAULT_CACHED_CHARACTER: Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'> = {
  userName: DEFAULT_CHARACTER.userName,
  userAvatar: DEFAULT_CHARACTER.userAvatar,
  aiName: DEFAULT_CHARACTER.aiName,
  aiAvatar: DEFAULT_CHARACTER.aiAvatar
};

class HistoryDb extends Dexie {
  /** 会话消息缓存�?*/
  messages!: Table<CachedMessage, number>;
  /** 按会话缓存的角色显示信息表（主键 session_id，含全局�?GLOBAL_SESSION_KEY�?*/
  character!: Table<CachedCharacter, string>;
  /** 本地持久化的会话列表占位表（用于恢复刷新前新建但尚未发消息的空会话） */
  sessions!: Table<CachedSessionMeta, string>;
  /** 进行�?agent 轮次的草稿缓存表（主�?[session_id+turn_num]，见 {@link DraftTurn}�?*/
  drafts!: Table<DraftTurn, [string, number]>;
  /** 用户上传的聊天区背景图表（主键 session_id，固定为全局行 {@link GLOBAL_SESSION_KEY}） */
  background!: Table<CachedBackground, string>;

  constructor() {
    super('ema-history-cache');
    this.version(1).stores({
      // 主键 id，复合索�?[session_id+turn_num] 用于按会话查�?去重�?
      // 单列 session_id 用于清空某会话的缓存�?
      messages: 'id, [session_id+turn_num], session_id'
    });
    this.version(2).stores({
      // �?session_id 主键缓存每个会话的头�?名字快照（含全局行）�?
      character: 'session_id'
    });
    this.version(3).stores({
      // 本地会话列表占位（主键为会话 id），新增表不破坏既有表结构�?
      sessions: 'id, updatedAt'
    });
    this.version(4).stores({
      // 复合主键 [session_id+turn_num]：整轮草稿按会话 + 轮次读写�?
      // 单列 session_id 用于删除某个会话时按前缀清空全部草稿�?
      drafts: '[session_id+turn_num], session_id'
    });
    this.version(5).stores({
      // 全局背景图（主键 session_id=GLOBAL_SESSION_KEY），新增表不破坏既有表结构�?
      background: 'session_id'
    });
    this.version(6).stores({
      // 结构不变，仅 messages 表新增 model_name/input_tokens/output_tokens 列，
      // 旧行缺省为 null，读取时按 undefined 处理即可。
      messages: 'id, [session_id+turn_num], session_id'
    }).upgrade((tx) => {
      return tx.table('messages').toCollection().modify((msg) => {
        if (msg.model_name === undefined) msg.model_name = null;
        if (msg.input_tokens === undefined) msg.input_tokens = null;
        if (msg.output_tokens === undefined) msg.output_tokens = null;
      });
    });
  }
}

/**
 * 全局唯一�?Dexie 数据库实例（用于缓存历史对话记录）�?
 */
export const db = new HistoryDb();

/**
 * 将服务端返回的消息行合并写入缓存（按 `id` 去重）�?
 *
 * @param rows  服务端返回的消息记录（含 `id` 字段�?
 * @returns     Promise，写入完成时 resolve
 */
export async function cacheMessages(rows: CachedMessage[]): Promise<void> {
  if (!rows || rows.length === 0) return;
  await db.messages.bulkPut(rows);
}

/**
 * 读取某个会话在本地缓存中的全部消息，并按 `turn_num` 升序、`id` 升序排列�?
 *
 * 通过复合索引 `[session_id+turn_num]` 做前缀查询：Dexie 会按复合�?
 * `(session_id, turn_num)` 升序返回结果，从而避免在 JS 中二次排序�?
 *
 * @param sessionId 会话 ID
 * @returns         缓存的该会话消息数组
 */
export async function readCachedMessages(sessionId: string): Promise<CachedMessage[]> {
  return await db.messages.where('[session_id+turn_num]').between([sessionId, MIN_KEY], [sessionId, MAX_KEY]).toArray();
}

/**
 * 计算某个会话在本地缓存中的最大轮次�?
 *
 * 复合索引 `[session_id+turn_num]` �?`turn_num` 升序返回该会话的行，
 * 因此最�?`turn_num` 可取最后一条记录的值，避免�?JS 中遍历全部行�?
 *
 * 若缓存为空则返回 `0`（后端要�?`min_turn_num >= 1`�?
 * 客户端不传该值上限，交由服务端逻辑处理）�?
 *
 * @param sessionId 会话 ID
 * @returns         缓存中的最�?turn_num（无缓存时为 0�?
 */
export async function cachedMaxTurnNum(sessionId: string): Promise<number> {
  const last = await db.messages
    .where('[session_id+turn_num]')
    .between([sessionId, MIN_KEY], [sessionId, MAX_KEY])
    .last();
  return last ? last.turn_num : 0;
}

/**
 * 清除某个会话在本地缓存中的全部消息�?
 *
 * @param sessionId 会话 ID
 */
export async function clearCachedSession(sessionId: string): Promise<void> {
  await db.messages.where('session_id').equals(sessionId).delete();
}

/**
 * 写入（缓�?/ 覆盖）某个会话的角色显示信息快照�?
 *
 * @param char 包含 `session_id`（真实会�?ID �?`GLOBAL_SESSION_KEY`）的角色信息
 */
export async function cacheCharacter(char: CachedCharacter): Promise<void> {
  await db.character.put(char);
}

/**
 * 读取某个会话的角色显示信息快照（无记录时返回 `undefined`）�?
 *
 * @param sessionId 会话 ID �?`GLOBAL_SESSION_KEY`
 */
export async function readCachedCharacter(sessionId: string): Promise<CachedCharacter | undefined> {
  return await db.character.get(sessionId);
}

/**
 * 清除某个会话的角色显示信息快照（删除会话时同步清理）�?
 *
 * @param sessionId 会话 ID（真实会话，不应�?`GLOBAL_SESSION_KEY`�?
 */
export async function clearCachedCharacter(sessionId: string): Promise<void> {
  await db.character.delete(sessionId);
}

/**
 * 写入（缓�?/ 覆盖）某个本地会话占位条目�?
 *
 * 新建空会话（尚未发消息）时服务端无记录，靠此表在刷新/重开后仍保留�?
 *
 * @param meta 会话占位条目（`id` 为会�?ID�?
 */
export async function cacheSessionMeta(meta: CachedSessionMeta): Promise<void> {
  await db.sessions.put(meta);
}

/**
 * 读取本地缓存中的全部会话占位条目，并�?`updatedAt` 降序（最新优先）排列�?
 *
 * @returns 本地缓存的会话占位数�?
 */
export async function readCachedSessionMetaList(): Promise<CachedSessionMeta[]> {
  const list = await db.sessions.toArray();
  return list.sort((a, b) => b.updatedAt - a.updatedAt);
}

/**
 * 删除本地缓存的某个会话占位条目（删除会话时同步清理；服务端已有记录时也无需保留占位）�?
 *
 * @param sessionId 会话 ID
 */
export async function clearCachedSessionMeta(sessionId: string): Promise<void> {
  await db.sessions.delete(sessionId);
}

/**
 * 写入（整体覆盖）某个会话某个轮次的进行中草稿�?
 *
 * 调用方每次状态变更（send / tool 各阶�?/ error / 落库对账前）都用「当前整�?
 * `MessageItem[]`」整体重写该行，从而实现「每执行一步即缓存」�?
 *
 * @param draft 待持久化的整轮草稿（�?`session_id` �?`turn_num`；`.messages` �?
 *              各条保留本地负临�?id，供对账时按内容匹配替换�?
 */
export async function saveDraftTurn(draft: DraftTurn): Promise<void> {
  await db.drafts.put(draft);
}

/**
 * 读取某个会话在本地缓存中的全部进行中草稿轮次，按 `turn_num` 升序排列�?
 *
 * 复合主键 `[session_id+turn_num]` 按轮次升序返回该会话的草稿；
 * `turn_num` 与实时消息同源（正轮次号），对账时可直接�?`session_id + turn_num +
 * role + content` 匹配、替换负临时 id 行，实现去重�?
 *
 * @param sessionId 会话 ID
 * @returns         该会话未完成的草稿轮数组（空数组表示无草稿）
 */
export async function readDraftTurns(sessionId: string): Promise<DraftTurn[]> {
  return await db.drafts.where('[session_id+turn_num]').between([sessionId, MIN_KEY], [sessionId, MAX_KEY]).toArray();
}

/**
 * 清除某个会话某个轮次的进行中草稿�?
 *
 * 在服务端成功落库（onDone �?重新拉取历史对账）或被用户主动停�?清空后调用，
 * 避免草稿与已落库消息重复渲染�?
 *
 * @param sessionId 会话 ID
 * @param turnNum   命中的草稿轮次（与实时消息同源的正轮次号�?
 */
export async function clearDraftTurn(sessionId: string, turnNum: number): Promise<void> {
  await db.drafts.delete([sessionId, turnNum]);
}

/**
 * 清除某个会话在本地缓存中的全部进行中草稿轮次�?
 *
 * 删除会话时同步清理，防止孤儿草稿在重建同 id 会话后错误重水合�?
 *
 * @param sessionId 会话 ID
 */
export async function clearDraftSession(sessionId: string): Promise<void> {
  await db.drafts.where('session_id').equals(sessionId).delete();
}

/**
 * 写入（覆盖）全局聊天区背景图�?
 *
 * @param backgroundUrl 背景图 base64 data URL（`data:image/...;base64,...`）；空字符串表示清除背景
 * @param backgroundOpacity 遮罩透明度（0-100）
 */
export async function saveBackground(
  backgroundUrl: string,
  backgroundOpacity: number
): Promise<void> {
  await db.background.put({
    session_id: GLOBAL_SESSION_KEY,
    backgroundUrl,
    backgroundOpacity
  });
}

/**
 * 读取全局聊天区背景配置（未设置时返回 `undefined`）�?
 *
 * @returns 背景配置 `{ backgroundUrl, backgroundOpacity }`；未设置时返回 undefined
 */
export async function readBackgroundConfig(): Promise<
  | { backgroundUrl: string; backgroundOpacity: number }
  | undefined
> {
  const row = await db.background.get(GLOBAL_SESSION_KEY);
  if (!row?.backgroundUrl) return undefined;
  return {
    backgroundUrl: row.backgroundUrl,
    backgroundOpacity: row.backgroundOpacity ?? 0
  };
}
