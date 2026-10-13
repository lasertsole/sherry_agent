/**
 * Legacy response envelope of the HTTP API.
 *
 * Older endpoints answered `{ code, data, msg }`; current endpoints answer with
 * the payload object itself (e.g. `{ jobs: [...] }`, `[...]`, `{ success: true }`),
 * which is why callers declare their payload through `fetchApi<T>` instead.
 * `data` is typed by `T` for the endpoints that still wrap their payload.
 */
export type Response<T = unknown> = {
  code?: number;
  data?: T;
  msg?: string;
};
