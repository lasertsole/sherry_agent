import type { MultiModalMessage } from "@/types/message";
import type { Response } from "@/types/response";
import type { SessionRecord } from "@/pages/home/type";
import { streamChatMessage, type OnChunkCallback, type OnDoneCallback, type OnHitlCallback, type StreamController, type HitlInterruptData } from './bridge';
import {
    cacheMessages,
    cachedMaxTurnNum,
    clearCachedSession,
    readCachedMessages,
    type CachedMessage,
} from './db';

/**
 * 删除会话时的「中止流式生成」事件名。
 *
 * 当一个会话被删除但该会话的 `[sid].vue` 仍被 KeepAlive 缓存（尤其非激活会话），
 * 其 WebSocket 流式生成可能仍在后台运行、持续向已删除的聊天状态推送内容。
 * 删除侧（home/index.vue）通过 mitt 广播本事件（负载为会话 id），
 * 对应的 `[sid].vue` 实例监听并 abort 其 AbortController 以停止流式生成。
 *
 * 该事件全程仅前端内存内广播，不触发任何服务端调用、不引入新依赖。
 */
export const SESSION_ABORT_STREAM_EVENT = 'session:abort-stream';

/**
 * 请求历史对话记录（本地 Dexie 缓存优先）。
 *
 * 每次请求会：
 * 1. 先从本地缓存读取该会话已有的消息，并取缓存中的最大 `turn_num`
 *    作为 `min_turn_num`（只向服务端请求缓存中缺失的新轮次）；
 * 2. 把服务端返回的增量消息合并写入缓存（按消息 `id` 去重）；
 * 3. 返回「缓存 + 增量」合并后的完整列表。
 *
 * @param session_id 会话ID
 * @param min_turn_num 最小轮次（>= 1；存在缓存时会被缓存最大轮次覆盖）
 * @param turn_page_size 每页轮次大小
 * @param turn_page_num 页码
 * @returns {Promise<CachedMessage[]>} 历史对话记录数组（本地缓存行的原样结构）
 */
export async function get_history_by_turn_page(session_id:string, min_turn_num:number, turn_page_size:number, turn_page_num:number):Promise<CachedMessage[]> {
    const cached = await readCachedMessages(session_id);
    // 用本地缓存的已有数据的最大 turn_num 作为 min_turn_num，
    // 只向服务端请求缓存中缺失的、更新轮次的数据；
    // 但调用方传入的 min_turn_num 优先（可用于覆盖缓存 max，加载更早历史/指定范围）。
    const cachedMinTurn = await cachedMaxTurnNum(session_id);
    // 服务端约定 min_turn_num >= 1；缓存为空（无最大轮次）时取调用方传入值，
    // 但仍需保证 >= 1（0 会被服务端 Pydantic 校验拒绝）。
    const effectiveMinTurn = cachedMinTurn > min_turn_num ? cachedMinTurn : Math.max(min_turn_num, 1);

    try {
        const res:Response = await fetchApi({
            url: '/get_history_by_turn_page',
            opts: {
                session_id,
                min_turn_num: effectiveMinTurn,
                turn_page_size,
                turn_page_num
            },
            method: 'get',
        });

        // 服务端 /get_history_by_turn_page 直接返回消息行数组（list[dict]），
        // 而不是 { data: [...] } 包装对象。这里做兼容：若响应本身就是数组则直接用，
        // 否则退化读取 res.data（兼容历史包装格式）。
        const fetched: CachedMessage[] = Array.isArray(res)
            ? (res as unknown as CachedMessage[])
            : (res.data || []);

        // 写入缓存（bulkPut 以 id 为主键去重）
        await cacheMessages(fetched);

        return mergeDedup(cached, fetched);
    } catch (error) {
        // 服务端请求失败时，回退返回本地缓存，保证离线可用
        return cached;
    };
};

/**
 * 按会话合并缓存与服务端返回的消息，并对 `id` 去重后按 `turn_num` 升序返回。
 */
function mergeDedup(
    cached: CachedMessage[],
    fetched: CachedMessage[],
): CachedMessage[] {
    const seen = new Map<number, CachedMessage>();
    for (const m of cached) seen.set(m.id, m);
    for (const m of fetched) seen.set(m.id, m); // 服务端数据覆盖缓存中的同 id 行
    return [...seen.values()].sort(
        (a, b) => a.turn_num - b.turn_num || a.id - b.id,
    );
}

/**
 * 清除会话历史
 * @param session_id 会话ID
 * @returns {Promise<boolean>} 清除成功返回 true
 */
export async function clearSession(session_id: string): Promise<boolean> {
    try {
        const res: Response = await fetchApi({
            url: '/sessions',
            opts: { session_id },
            method: 'delete',
        });
        await clearCachedSession(session_id);
        return true;
    } catch (error) {
        return false;
    }
}

/**
 * 从服务端拉取全部会话列表，按最近活动倒序。
 *
 * 对应服务端 GET /sessions（server/trigger/http/messages.py），返回
 * ``[{session_id, last_time, title}]``。这里映射为前端的 ``SessionRecord``。
 *
 * @returns {Promise<SessionRecord[]>} 会话记录数组；请求失败时返回空数组
 */
export async function getSessionList(): Promise<SessionRecord[]> {
    try {
        const res: Response = await fetchApi({
            url: '/sessions',
            method: 'get',
        });
        // 服务端 /sessions 直接返回数组，而非 { data: [...] } 包装对象。
        // 这里做兼容：若响应本身就是数组则直接用，否则退化读取 res.data。
        const rows: Array<{ session_id: string; last_time: string; title: string }> =
            Array.isArray(res)
                ? (res as unknown as Array<{ session_id: string; last_time: string; title: string }>)
                : (res.data || []);
        return rows.map((row) => ({
            id: row.session_id,
            title: row.title ?? row.session_id,
            createTime: row.last_time,
        }));
    } catch (error) {
        // 请求失败时返回空列表，避免阻断会话列表加载
        return [];
    }
}

/**
 * 查询指定会话是否存在「待人工审批」的 HITL 中断。
 *
 * 对应服务端 GET /get_pending_interrupt（server/trigger/http/messages.py）。
 * 服务端从 LangGraph checkpoint 重推 `{tool_name, tool_args, description, allowed_decisions}`，
 * 无中断时返回 null。用于在会话切换/页面刷新/浏览器重开/服务重启后
 * 重新拉起待审批的批准卡片。
 *
 * @param session_id 会话ID
 * @returns 待审批的 HITL 中断数据；无中断或请求失败时返回 null
 */
export async function getPendingInterrupt(
  session_id: string,
): Promise<HitlInterruptData | null> {
  try {
    const res: Response = await fetchApi({
      url: '/get_pending_interrupt',
      opts: { session_id },
      method: 'get',
    });
    if (res == null) return null;
    // 服务端直接返回中断对象（或 null），不做 { data } 包装；此处做兼容。
    const data = (res as unknown as { data?: unknown }).data ?? res;
    // 兼容兜底：服务端可能以 text/plain 返回字面量字符串 "None"（Python None），
    // ofetch 不会对其做 JSON 解析，此时 data 是一段 truthy 的字符串，必须视为「无中断」，
    // 否则会误弹出一个 tool_name 全空的失效 HITL 卡（尤其空会话一进来就触发）。
    if (
      data == null ||
      typeof data !== 'object' ||
      Array.isArray(data) ||
      data['None'] === true
    ) {
      return null;
    }
    return data as HitlInterruptData;
  } catch (error) {
    // 请求失败（会话可能已清空/后端未起）时静默视为无中断，不阻断聊天。
    console.warn('[getPendingInterrupt] 查询待审批中断失败：', error);
    return null;
  }
}

/**
 * 流式请求 AI 回复，桥接到统一的 WebSocket 通路
 * (对应 server/trigger/ws/messages.py `/sessions/agent/ws`)
 *
 * 浏览器模式经由 WebSocket 接收流式块；Tauri 模式经由 IPC + Tauri Events。
 * 与旧的（已失效的）`/sessions/agent/sse` HTTP 端点解耦。
 *
 * @param session_id 会话ID
 * @param multi_modal_message 用户输入 { text, image_base64_list?, audio_bytes_list?, video_bytes_list? }
 * @param onData 每块文本回调（携带语义类型：text / tool_start / tool_end）
 * @param onDone 流结束回调
 * @param onError 出错回调
 * @returns {AbortController} 外部可通过 controller.abort() 中止请求
 */
export function postAgentStream(
    session_id: string,
    multi_modal_message: MultiModalMessage,
    onData: OnChunkCallback,
    onDone?: OnDoneCallback,
    onError?: (err: unknown) => void,
    onHitl?: OnHitlCallback,
): AbortController {
    const controller = new AbortController();
    let stopFn: (() => void) | null = null;
    let hitlSender: (((response: import('./bridge').HitlResponse) => void) | null) = null;

    // 桥接到 bridge 的统一流式入口（浏览器 WS / Tauri IPC）。
    const { controller: stream, promise } = streamChatMessage(
        {
            session_id,
            text: multi_modal_message.text ?? '',
            image_base64_list: multi_modal_message.image_base64_list,
            audio_bytes_list: multi_modal_message.audio_bytes_list,
            video_bytes_list: multi_modal_message.video_bytes_list,
        },
        onData,
        onHitl,
        onDone,
    );
    stopFn = () => stream.abort();
    hitlSender = stream.sendHitlResponse ?? null;

    // 将 sendHitlResponse 挂载到返回的 AbortController 上
    (controller as any).sendHitlResponse = hitlSender;

    // 用户主动 abort → 触发流式止停
    controller.signal.addEventListener('abort', () => stopFn?.());

    promise
        .then(() => {
            // onDone 由 streamChatMessage 在流正常结束时统一触发（携带模型元数据），
            // 此处不再重复调用，避免在浏览器 WS 模式下被回调两次。
        })
        .catch((err) => {
            // 主动中止（abort/stop）不算业务错误，不触发 onError
            const message = err instanceof Error ? err.message : String(err);
            if (message === 'aborted') {
                controller.abort();
                return;
            }
            onError?.(err);
        });

    return controller;
}