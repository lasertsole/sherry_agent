import type { Response } from "@/types/response";

export interface EnvEntry {
  key: string;
  value: string;
  value_edited: boolean;
}

export interface EnvGroup {
  name: string;
  entries: EnvEntry[];
}

/** 后端 `/env` 返回的原始结构（分组列表） */
export interface EnvConfigPayload {
  groups: EnvGroup[];
}

/**
 * 读取项目 .env 配置（按前缀分组）。
 * 请求失败时抛出异常，交由调用方区分「请求错误」与「真实空 .env」，
 * 避免把网络/鉴权失败误报为「未找到 .env 文件」。
 */
export async function readEnvConfig(): Promise<EnvConfigPayload> {
  // Backend `GET /env` returns the group payload directly (NOT wrapped in
  // { code, data, msg }). So the resolved response IS `{ groups: [...] }`.
  //
  // Caller-context requirement: `fetchApi` is built on Nuxt's `useFetch` with
  // `server:true`. In a pure SPA, `useFetch` only issues a real request when
  // called from a live setup context (getCurrentInstance() != null). Calling it
  // from an event handler (e.g. PrimeVue TabPanel `@show`) yields `data ===
  // undefined` without any network round-trip. The caller MUST trigger this
  // from setup scope (see ConfigDialog.vue's setup-context watch), otherwise the
  // `|| { groups: [] }` fallback below misleads the user into thinking the
  // `.env` file is missing.
  const res = await fetchApi({
    url: '/env',
    opts: { _ts: Date.now() },
    method: 'get',
  });
  // `res` is never null in practice on success (backend responds with the full
  // payload). Guard against an empty body while preserving the `{ groups }`
  // shape so a non-throw path never yields an unreadable value.
  return (res as unknown as EnvConfigPayload | undefined) || { groups: [] };
}

/**
 * 更新 .env 中已存在 key 的值。
 * @param changes 形如 { KEY: "new value" }；仅接受文件中已存在的 key。
 * @returns 成功 true，失败 false
 */
export async function writeEnvConfig(changes: Record<string, string>): Promise<boolean> {
  try {
    await fetchApi({
      url: '/env',
      opts: { changes },
      method: 'put',
    });
    return true;
  } catch (error) {
    return false;
  }
}
