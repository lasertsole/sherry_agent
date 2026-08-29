import type { NitroFetchRequest } from 'nitropack';
import type { Response } from '~/types/response';
import { sendRequestErrorToast } from './toast';

interface Params {
  url: NitroFetchRequest;
  opts?: { [key: string]: any } | FormData;
  method?: 'get' | 'post' | 'put' | 'patch' | 'delete';
  contentType?: 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data';
  lazy?: boolean;
  headeropts?: { [key: string]: any };
  onError?: () => void;
  initialCache?: boolean;
  server?: boolean;
  watch?: [];
}

/**
 * 替换路径变量
 *
 * @param { NitroFetchRequest } url 请求路径
 * @param { any } params 路径参数
 * @returns { NitroFetchRequest } 替换后的请求路径
 */
const replacePathVariables = (url: NitroFetchRequest, params: any = {}): NitroFetchRequest => {
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
    if (params[m[1]] === undefined) {
      throw new Error(`"${m[1]}" is not provided in params`);
    }
    formattedURL = formattedURL.replace(`:${m[1]}`, params[m[1]]);
    delete params[m[1]];
    m = regex.exec(formattedURL);
  }
  return formattedURL;
};

// tus上传参数
interface UploadParams {
  url: string;
  file: File;
  progressCB?: Function;
  successCB?: Function;
  errorCB?: Function;
}

/**
 * 有服务器渲染功能的请求
 * @param { NitroFetchRequest } url 请求路径
 * @param { {[key: string]: any} | FormData } opts 请求参数
 * @param { 'get' | 'post' | 'put' | 'delete' } method 请求方法
 * @param { 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data' } contentType 请求内容类型
 * @param { {[key: string]: any} } headeropts 请求头参数
 * @param { boolean } server 是否服务器渲染
 * @param { Array<()=>void> } watch 监测是否需要重新请求
 * @returns {Promise<Response>} 请求结果
 */
async function requestBaseApi({
  url,
  opts = {},
  method = 'get',
  contentType = 'application/json',
  headeropts = {}
}: Params): Promise<Response> {
  const requestURL = replacePathVariables(url, opts);

  // 设置请求参数
  const params: any = {};
  if (contentType == 'application/json') {
    opts = { ...opts };
  }

  if (method == 'get') {
    params.query = opts;
  } else {
    params.body = opts;
  }

  // 网络/HTTP 失败标志位：ofetch 的 retry:3 会对每次失败重试都触发
  // onRequestError/onResponseError 回调，若在回调内直接弹 toast 会重复弹多次。
  // 故回调里只记录标志位，在请求结束后统一判定一次（单次 toast）。
  let networkFailed = false;
  let httpFailed = false;
  let lastStatus: number | null = null;

  // 使用 $fetch（而非 useFetch）：本包装器仅在挂载后（事件回调/组合式函数）被调用，
  // useFetch 在此场景会触发 NUXT_E3003 警告（无法在 setup 中 await），且需靠每次生成
  // 唯一 key 规避 useAsyncData 缓存。$fetch 即 useFetch 底层的 ofetch 实例，拦截器与
  // retry 语义完全一致，却无缓存与 setup 时机约束。
  // 注意 $fetch 失败会 throw，而原 useFetch 语义是 resolve(null)；为保持调用方按
  // 空值处理的契约（外层 retryFetch 仅对显式抛错如路径参数缺失重试），此处捕获异常
  // 并落为 null，失败信息由 onResponseError 标志位 + 统一 toast 表达。
  let data: Response | null;
  try {
    data = await $fetch<Response>(requestURL, {
      method,
      // ofetch库会自动识别请求地址，对于url已包含域名的请求不会再拼接baseURL
      // 与 ws.ts / bridge.ts 的流式路径保持一致的回退：当 VITE_API_BACK_URL 未配置时
      // 兜底到本地后端地址，避免请求退化为相对路径而命中 Nuxt dev server 返回 HTML 外壳。
      baseURL: import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080',
      ...params,
      retry: 3,
      retryDelay: 2000,
      // onRequest相当于请求拦截
      onRequest({ request, options }) {
        // 设置请求头（GET请求不需要Content-Type）
        // 注意：multipart/form-data 不能手动设置 Content-Type，否则会覆盖掉
        // fetch/ofetch 自动生成的 boundary，导致后端解析失败（boundary is not found）。
        // 正确做法是交给浏览器自动生成 Content-Type（含 boundary）。
        if (method !== 'get' && contentType !== 'multipart/form-data') {
          options.headers.set('Content-Type', contentType);
        }
        for (const [key, value] of Object.entries(headeropts)) {
          options.headers.set(key, value);
        }

        if (import.meta.client) {
          const token = localStorage.getItem('token');
          if (token) {
            options.headers.set('token', token);
          }
        }
      },

      onRequestError({ request, options, error }) {
        // 网络层失败（DNS 解析失败/连接被拒/断网等，请求从未到达服务器）。
        // 只记录标志位；toast 由请求结束后统一判定弹出（retry 期间会多次进入此回调）。
        networkFailed = true;
      },

      // onResponse相当于响应拦截
      onResponse({ response }) {
        // 处理响应数据
        // 本次尝试收到了响应（无论状态码）：重置此前尝试累积的失败标志位。
        // 语义：若最终一次尝试成功，则之前重试期间的 networkFailed/httpFailed 不再生效，
        // 请求结束后的判定不会弹出错误 toast。
        networkFailed = false;
        httpFailed = false;
        if (import.meta.client) {
          // 如果返回值有token，则更新本地token
          const token: string | null = response.headers.get('token');
          if (token) {
            localStorage.setItem('token', token);
          }

          return response;
        }
      },

      onResponseError({ request, response, options }) {
        // HTTP 级失败（4xx/5xx）：记录标志位与最终状态码；toast 由请求结束后统一判定。
        // 注意每次失败重试都会进入此回调，且 onResponse（先触发）会重置标志位，
        // 故最终一次尝试失败时标志位仍会正确落为 true。
        httpFailed = true;
        lastStatus = response?.status ?? null;
      }
    });
  } catch {
    // 对齐原 useFetch 语义：失败不向调用方抛出，落为 null 由调用方按空值处理。
    data = null;
  }

  // 重试穷尽仍失败 → 弹一次全局错误 toast（网络错误；或 HTTP 错误且未拿到成功数据）。
  // 这里是单次判定点：回调内的标志位不会直接触发 toast，避免 retry:3 重复弹窗。
  if (import.meta.client && (networkFailed || (httpFailed && !data))) {
    sendRequestErrorToast(`${requestURL}${lastStatus !== null ? ` (HTTP ${lastStatus})` : ''}`);
  }

  return data as Response;
}

/**
 * 封装请求重试
 *
 * @param { ()=>Promise<Response> } fetchFunc 请求函数
 * @param { number } retryMaxCount 最大重试次数
 * @param { number } retryDelay 每次重试的延迟时间,单位毫秒
 * @returns {Promise<Response>} 响应对象
 */
function retryFetch(
  fetchFunc: () => Promise<Response>,
  retryMaxCount: number = 3,
  retryDelay: number = 1000
): Promise<Response> {
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

/**
 * 请求api
 *
 * @param { NitroFetchRequest } url 请求路径
 * @param { [key: string]: any | FormData } opts 请求参数
 * @param { 'get' | 'post' | 'put' | 'delete' } method 请求方法
 * @param { 'application/x-www-form-urlencoded' | 'application/json' | 'multipart/form-data' } contentType 请求内容类型
 * @param { [key: string]: any } headeropts 请求头参数
 * @returns {Promise<Response>} 请求结果
 */
export async function fetchApi({
  url,
  opts = {},
  method = 'get',
  contentType = 'application/json',
  headeropts = {}
}: Params): Promise<Response> {
  return retryFetch(() =>
    requestBaseApi({
      url,
      opts,
      method,
      contentType,
      headeropts
    })
  );
}
