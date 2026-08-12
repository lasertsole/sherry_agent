import type { MultiModalMessage } from "@/types/message";
import type { Response } from "@/types/response";
import { streamChatMessage, type OnChunkCallback, type OnHitlCallback, type StreamController } from './bridge';
import {
    cacheMessages,
    cachedMaxTurnNum,
    clearCachedSession,
    readCachedMessages,
    type CachedMessage,
} from './db';

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
 * (对应 client/api/core.py clear_session)
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
    onDone?: () => void,
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
        },
        onData,
        onHitl,
    );
    stopFn = () => stream.abort();
    hitlSender = stream.sendHitlResponse ?? null;

    // 将 sendHitlResponse 挂载到返回的 AbortController 上
    (controller as any).sendHitlResponse = hitlSender;

    // 用户主动 abort → 触发流式止停
    controller.signal.addEventListener('abort', () => stopFn?.());

    promise
        .then(() => {
            onDone?.();
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