import type { MultiModalMessage } from "@/types/message";
import type { Response } from "@/types/response";
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
 * @param min_turn_num 最小轮次（缓存为空时传 0；存在缓存时会被缓存最大轮次覆盖）
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
    const effectiveMinTurn = cachedMinTurn > min_turn_num ? cachedMinTurn : min_turn_num;

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

        const fetched: CachedMessage[] = res.data || [];
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
 * SSE 流式请求 AI 回复
 * (对应 client/api/core.py post_agent_astream)
 *
 * 通过 EventSource / fetch ReadableStream 接收 SSE 事件流，
 * 每收到一个 data 块就调用 onData 回调，stream 结束后调用 onDone。
 *
 * @param session_id 会话ID
 * @param multi_modal_message 用户输入 { text, image_base64_list?, audio_bytes_list?, video_bytes_list? }
 * @param onData 每块 SSE data 的回调
 * @param onDone 流结束回调
 * @param onError 出错回调
 * @returns {AbortController} 外部可通过 controller.abort() 中止请求
 */
export function postAgentStream(
    session_id: string,
    multi_modal_message: MultiModalMessage,
    onData: (chunk: string) => void,
    onDone?: () => void,
    onError?: (err: unknown) => void,
): AbortController {
    const controller = new AbortController();
    const baseUrl = import.meta.env.VITE_API_BACK_URL || '';

    (async () => {
        try {
            const response = await fetch(`${baseUrl}/sessions/agent/sse`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id, multi_modal_message }),
                signal: controller.signal,
            });

            if (!response.ok) {
                throw new Error(`SSE request failed: ${response.status}`);
            }

            const reader = response.body?.getReader();
            if (!reader) {
                throw new Error('Response body is not readable');
            }

            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                // SSE lines: 逐行解析，提取 "data: " 前缀的内容
                const lines = buffer.split('\n');
                buffer = lines.pop() || ''; // 最后一个不完整片段保留

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = line.slice(6).trim();
                        if (data) {
                            onData(data);
                        }
                    }
                }
            }

            onDone?.();
        } catch (err: unknown) {
            if (err instanceof DOMException && err.name === 'AbortError') {
                // 主动中止，不触发 onError
                return;
            }
            onError?.(err);
        }
    })();

    return controller;
}