import type { Response, UseFetchResponse } from "@/types/response";

/**
 * 请求历史对话记录
 * @returns {Promise<object[]>} 历史对话记录数组
 */
export async function get_history_turn_message(session_id:string, last_turn_count:number):Promise<object[]> {
    try {
        const res:Response = await fetchApi({
            url: '/n_turns_history_messages',
            opts: {
                session_id,
                last_turn_count
            },
            method: 'get',
        });
        return res.data || [];
    } catch (error) {
        return [];
    };
};