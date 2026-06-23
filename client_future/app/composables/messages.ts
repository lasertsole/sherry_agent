import type { BaseMessage, MultiModalMessage } from "@/types/message";
import type { Response, UseFetchResponse } from "@/types/response";

/**
 * 请求历史对话记录
 * @param session_id 会话ID
 * @param min_turn_num 最小轮次
 * @param turn_page_size 每页轮次大小
 * @param turn_page_num 页码
 * @returns {Promise<BaseMessage[]>} 历史对话记录数组
 */
export async function get_history_by_page(session_id:string, min_turn_num:number, turn_page_size:number, turn_page_num:number):Promise<BaseMessage[]> {
    try {
        const res:Response = await fetchApi({
            url: '/get_history_by_page',
            opts: {
                session_id,
                min_turn_num,
                turn_page_size,
                turn_page_num
            },
            method: 'get',
        });
        return res.data || [];
    } catch (error) {
        return [];
    };
};

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