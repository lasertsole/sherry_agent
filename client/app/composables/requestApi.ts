import type { NitroFetchRequest } from 'nitropack';
import type { Response } from '~/types/response';
import { logUtil } from '~/utils/log';

interface Params {
  url: NitroFetchRequest;
  opts?: { [key: string]: unknown } | FormData;
  method?: 'get' | 'post' | 'put' | 'patch' | 'delete';
  contentType?: 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data';
  lazy?: boolean;
  headeropts?: { [key: string]: unknown };
  onError?: () => void;
  initialCache?: boolean;
  server?: boolean;
  watch?: [];
}

/**
 * In-memory token.
 *
 * The token previously lived in `localStorage`, where any XSS payload could
 * read it persistently. It is now held only in module scope: it survives SPA
 * navigation, dies with the tab, and is wiped by the legacy cleanup below.
 * Transport stays the same custom `token` header so the backend contract is
 * unchanged; httpOnly-cookie issuance lands with the auth middleware (P1, audit #3).
 */
let memoryToken: string | null = null;

/** One-time removal of the legacy localStorage token left by older builds. */
function purgeLegacyToken(): void {
  try {
    localStorage.removeItem('token');
  } catch {
    // Storage unavailable (privacy mode / Tauri restriction) — nothing to purge.
  }
}

let legacyTokenPurged = false;

function consumeLegacyToken(): void {
  if (legacyTokenPurged) return;
  legacyTokenPurged = true;
  purgeLegacyToken();
}

/**
 * Replace path variables
 *
 * @param { NitroFetchRequest } url Request path
 * @param { any } params Path parameters
 * @returns { NitroFetchRequest } The request path after replacement
 */
const replacePathVariables = (url: NitroFetchRequest, params: Record<string, unknown> = {}): NitroFetchRequest => {
  if (Object.keys(params).length === 0) {
    return url;
  }
  const regex = /\/:(\w+)/gm;
  let formattedURL = url as string;
  let m = regex.exec(formattedURL);
  while (m) {
    if (m.index === regex.lastIndex) {
      regex.lastIndex += 1;
    }
    // Capture group 1 (\w+) always exists in a successful match; guard satisfies noUncheckedIndexedAccess
    const key = m[1];
    if (!key) break;
    if (params[key] === undefined) {
      throw new Error(`"${key}" is not provided in params`);
    }
    formattedURL = formattedURL.replace(`:${key}`, String(params[key]));
    delete params[key];
    m = regex.exec(formattedURL);
  }
  return formattedURL;
};

/**
 * Narrow payload guard for a resolved API response (audit #51).
 *
 * The backend answers with a JSON object/array, or — on legacy endpoints such as
 * `/get_pending_interrupt` — a bare `text/plain` string (e.g. `"None"`). Any
 * other shape (number/boolean/undefined) cannot satisfy the `Response` contract
 * the callers consume, so it is rejected at this boundary instead of being
 * forced through `as Response`.
 *
 * The caller declares the concrete payload type via `T`; this guard proves the
 * wire value is a JSON container (object/string), not the declared shape.
 * @param value Value resolved by ofetch
 * @returns True when the value is a JSON container (object or string).
 */
function isApiPayload<T>(value: unknown): value is T {
  if (value === null) return false;
  if (typeof value === 'string') return true;
  return typeof value === 'object';
}

/**
 * Frontend error-handling strategy — the boundary toast decision.
 *
 * Policy: the HTTP client (`fetchApi`) is the ONLY layer allowed to raise a
 * generic request-error toast; domain callers either fall back silently
 * (cache/default) or render the failure on their own surface (chat bubble),
 * and MUST NOT toast on top of it. This predicate is the single decision point,
 * evaluated once after the retry loop: a toast fires when the final state is a
 * network failure, an unexpected/thrown failure, or an HTTP 4xx/5xx with no
 * usable payload — while a payload resolved by a later retry suppresses it.
 * @param flags
 * @param flags.networkFailed
 * @param flags.requestFailed
 * @param flags.httpFailed
 * @param flags.hasData
 */
export function shouldRequestFailureToast(flags: {
  /** The request never reached the server (network layer). */
  networkFailed: boolean;
  /** An unexpected/thrown failure (interceptor throw, unparsable body, abort). */
  requestFailed: boolean;
  /** The final attempt answered with HTTP 4xx/5xx. */
  httpFailed: boolean;
  /** A usable payload was resolved. */
  hasData: boolean;
}): boolean {
  return flags.networkFailed || flags.requestFailed || (flags.httpFailed && !flags.hasData);
}

/**
 * Request with server-side rendering support
 * @param { NitroFetchRequest } url Request path
 * @param { object } opts Request parameters
 * @param { 'get' | 'post' | 'put' | 'patch' | 'delete' } method Request method
 * @param { 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data' } contentType Request content type
 * @param { {[key: string]: any} } headeropts Request header parameters
 * @param { boolean } server Whether server-side rendering is used
 * @param { Array<()=>void> } watch Watch for whether a re-request is needed
 * @template T Payload type declared by the caller
 * @returns Request result; null means the request failed
 */

async function requestBaseApi<T = Response>({
  url,
  opts = {},
  method = 'get',
  contentType = 'application/json',
  headeropts = {}
}: Params): Promise<T | null> {
  const requestURL = opts instanceof FormData ? url : replacePathVariables(url, opts);

  // Set up request parameters
  const params: Record<string, unknown> = {};
  if (contentType == 'application/json') {
    opts = { ...opts };
  }

  if (method == 'get') {
    params.query = opts;
  } else {
    params.body = opts;
  }

  // Network/HTTP failure flags: ofetch's retry:3 triggers the onRequestError/onResponseError
  // callbacks on every failed attempt; toasting directly inside the callbacks would pop up
  // duplicate toasts. Therefore the callbacks only record flags, and a single unified check
  // happens after the request ends (one toast).
  let networkFailed = false;
  let httpFailed = false;
  let requestFailed = false;
  let lastStatus: number | null = null;

  // Use $fetch (not useFetch): this wrapper is only called after mount (event callbacks/composables),
  // where useFetch would trigger the NUXT_E3003 warning (cannot await in setup) and would need a
  // unique key per call to bypass the useAsyncData cache. $fetch is the underlying ofetch instance
  // used by useFetch, with identical interceptor and retry semantics, but without cache or
  // setup-timing constraints.
  // Note that $fetch throws on failure, whereas the original useFetch semantics resolve(null); the
  // outer retryFetch only retries on explicit throws such as missing path parameters, so exceptions
  // are caught here and turned into null; failure info is conveyed by the flags + the unified toast.
  let data: T | null;
  try {
    const raw = await $fetch<unknown>(requestURL, {
      method,
      // The ofetch library auto-detects the request URL; for requests whose url already contains a
      // domain, baseURL is not prepended. The fallback (local backend address when VITE_API_BACK_URL
      // is not configured) is shared with the streaming paths via API_BASE_URL (env.ts).
      baseURL: API_BASE_URL,
      ...params,
      retry: 3,
      retryDelay: 2000,
      // onRequest is equivalent to a request interceptor
      onRequest({ options }) {
        // Set request headers (GET requests don't need Content-Type)
        // Note: multipart/form-data must not have its Content-Type set manually, otherwise it would
        // override the boundary auto-generated by fetch/ofetch and break backend parsing
        // ("boundary is not found").
        // The correct approach is to let the browser auto-generate the Content-Type (with boundary).
        if (method !== 'get' && contentType !== 'multipart/form-data') {
          options.headers.set('Content-Type', contentType);
        }
        for (const [key, value] of Object.entries(headeropts)) {
          options.headers.set(key, String(value));
        }

        if (import.meta.client) {
          consumeLegacyToken();
          if (memoryToken) {
            options.headers.set('token', memoryToken);
          }
        }
      },

      onRequestError() {
        // Network-layer failure (DNS resolution failure/connection refused/offline, etc.; the request
        // never reached the server).
        // Only records the flag; the toast is shown by the unified check after the request ends
        // (this callback is entered multiple times during retry).
        networkFailed = true;
      },

      // onResponse is equivalent to a response interceptor
      onResponse({ response }) {
        // Handle response data
        // This attempt received a response (regardless of status code): reset the failure flags
        // accumulated by prior attempts.
        // Semantics: if the final attempt succeeds, the networkFailed/httpFailed flags accumulated
        // during earlier retries no longer apply, and the post-request check pops no error toast.
        networkFailed = false;
        httpFailed = false;
        if (import.meta.client) {
          // If the response issues a token, hold it in memory only (never persisted).
          const token: string | null = response.headers.get('token');
          if (token) {
            memoryToken = token;
          }
        }
      },

      onResponseError({ response }) {
        // HTTP-level failure (4xx/5xx): record the flag and the final status code; the toast is
        // shown by the unified check after the request ends.
        // Note this callback is entered on every failed retry, and onResponse (triggered first)
        // resets the flags, so when the final attempt fails the flags still correctly end up true.
        httpFailed = true;
        lastStatus = response?.status ?? null;
      }
    });

    // Runtime boundary check (audit #51): reject payloads that cannot satisfy
    // the Response contract before callers consume them.
    data = isApiPayload<T>(raw) ? raw : null;
    if (data === null) {
      requestFailed = true;
      logUtil.e(`[requestApi] Unexpected response payload from ${String(requestURL)}:`, raw);
    }
  } catch (error) {
    // No longer swallowed (audit #53): a failure that is neither a network error
    // nor a 4xx/5xx (interceptor throw, unparsable body, abort) is recorded and
    // logged here, then reported through the same single-toast path below. The
    // caller still receives null (null = request failed; a Response whose `data`
    // is missing/empty = an empty result).
    requestFailed = true;
    data = null;
    logUtil.e(`[requestApi] Request failed: ${String(requestURL)}`, error);
  }

  // Retries exhausted and still failing → pop one global error toast (network error; HTTP error
  // with no successful data; or an unexpected/thrown failure).
  // This is the single decision point (see `shouldRequestFailureToast`): the flags inside the
  // callbacks never trigger a toast directly, avoiding duplicate toasts from retry:3.
  if (
    import.meta.client &&
    shouldRequestFailureToast({ networkFailed, requestFailed, httpFailed, hasData: data !== null })
  ) {
    sendRequestErrorToast(`${requestURL}${lastStatus !== null ? ` (HTTP ${lastStatus})` : ''}`);
  }

  return data;
}

/**
 * Wrap a request with retries
 *
 * @param { ()=>Promise<T | null> } fetchFunc Request function
 * @param { number } retryMaxCount Maximum number of retries
 * @param { number } retryDelay Delay between retries, in milliseconds
 * @template T Payload type declared by the caller
 * @returns The response object, or null when the request failed
 */
function retryFetch<T>(
  fetchFunc: () => Promise<T | null>,
  retryMaxCount: number = 3,
  retryDelay: number = 1000
): Promise<T | null> {
  return fetchFunc().catch(err => {
    if (retryMaxCount <= 0) {
      return Promise.reject(err);
    } else {
      return new Promise((resolve, reject) => {
        setTimeout(() => {
          retryFetch(fetchFunc, retryMaxCount - 1, retryDelay)
            .then(resolve)
            .catch(reject);
        }, retryDelay);
      });
    }
  });
}

/* eslint-disable jsdoc/check-param-names -- plugin cannot bind a destructured parameter that carries an object-literal default */
/**
 * Request API
 *
 * The caller declares the payload shape it consumes through `T`; the wire value
 * is validated as a JSON container at this boundary (see `isApiPayload`).
 *
 * @param { NitroFetchRequest } url Request path
 * @param { object } opts Request parameters
 * @param { 'get' | 'post' | 'put' | 'patch' | 'delete' } method Request method
 * @param { 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data' } contentType Request content type
 * @param { [key: string]: any } headeropts Request header parameters
 * @template T Payload type declared by the caller
 * @returns Request result; null means the request failed
 */
export async function fetchApi<T = Response>({
  url,
  opts = {},
  method = 'get',
  contentType = 'application/json',
  headeropts = {}
}: Params): Promise<T | null> {
  return retryFetch<T>(() =>
    requestBaseApi<T>({
      url,
      opts,
      method,
      contentType,
      headeropts
    })
  );
}

/**
 * Request an endpoint whose payload the caller declares and consumes as always
 * present.
 *
 * The bridge modules historically promised a non-null payload to their callers
 * (their result was force-cast), while `fetchApi` resolves null on failure. This
 * keeps that exact contract — a failed request still passes the null through
 * unchanged — with the payload type declared in one place instead of an
 * `as unknown as` cast at every call site.
 * @param params Request parameters (same shape as `fetchApi`).
 * @returns The resolved payload; on failure, null (as `fetchApi` resolved it).
 */
export async function fetchApiPayload<T = Response>(params: Params): Promise<T> {
  const payload = await fetchApi<T>(params);
  // The declared payload of a successful request; the failure null is passed
  // through unchanged to preserve the legacy bridge contract.
  return payload as T;
}

/** Options for the raw-response transport entry {@link fetchApiRaw}. */
export interface RawFetchOptions {
  /** Path relative to the shared API base URL (e.g. '/system_prompt'). */
  url: string;
  method?: 'get' | 'post' | 'put' | 'patch' | 'delete';
  /** Raw request body (binary media uploads); omitted for bodyless requests. */
  body?: BodyInit;
  /** Explicit Content-Type header; omitted lets the browser infer it. */
  contentType?: string;
  signal?: AbortSignal;
}

/**
 * Raw-response transport: the SAME API base URL and token policy as
 * `fetchApi`, but deliberately NO retry, NO failure toast and NO body parsing —
 * it returns the untouched `Response`, and a transport failure rejects with the
 * underlying fetch error unchanged.
 *
 * This exists for the callers that own their own failure contract
 * (`bridge/upload.ts` throws labelled upload errors after inspecting
 * status/non-JSON body; `bridge/health.ts` maps failures to
 * `{ healthy: false, message }` and must never raise a user-visible toast).
 * Routing them through `fetchApi` (ofetch retry:3 + global toast + parsed
 * `null`) would change their observable failure behavior.
 * @param options
 */
export async function fetchApiRaw(options: RawFetchOptions): Promise<globalThis.Response> {
  const headers = new Headers();
  if (options.contentType) headers.set('Content-Type', options.contentType);
  if (import.meta.client) {
    consumeLegacyToken();
    if (memoryToken) {
      headers.set('token', memoryToken);
    }
  }
  return fetch(`${API_BASE_URL}${options.url}`, {
    method: options.method ?? 'get',
    headers,
    body: options.body,
    signal: options.signal
  });
}
