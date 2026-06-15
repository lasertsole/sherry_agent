import type { FetchError } from 'ofetch';

export type Response = {
    code?: number,
    data?: any,
    msg?: string
};

export type UseFetchResponse = {
    data:Ref,
    error:Ref<FetchError<any>| null, FetchError<any> | null>,
    status:Ref<string>,
    refresh:() => Promise<void>,
    clear:() => void
};