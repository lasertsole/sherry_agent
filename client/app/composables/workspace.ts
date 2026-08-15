import type { BaseMessage, MultiModalMessage } from "@/types/message";
import type { Response, UseFetchResponse } from "@/types/response";

export async function read_system_prompt_handler():Promise<Record<string, string>> {
    try {
        const res:Response = await fetchApi({
            url: '/system_prompt',
            opts: {},
            method: 'get',
        });
        return res.data || {};
    } catch (error) {
        return {};
    };
};

export async function write_system_prompt_file_handler(file_to_content:Record<string, string>):Promise<boolean> {
    try {
        const res:Response = await fetchApi({
            url: '/system_prompt',
            opts: { file_to_content },
            method: 'post',
        });
        return true;
    } catch (error) {
        return false;
    }
};

export async function update_system_prompt_file_handler(file_to_content:Record<string, string>):Promise<boolean> {
        try {
        const res:Response = await fetchApi({
            url: '/system_prompt',
            opts: { file_to_content },
            method: 'patch',
        });
        return true;
    } catch (error) {
        return false;
    }
};