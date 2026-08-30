import type { ToastMessageOptions } from 'primevue/toast';
import type { ToastServiceMethods } from 'primevue/toastservice';
import { resolveRuntimeT } from '~/composables/i18nRuntime';

/**
 * 全局 toast 通知层。
 *
 * 设计约束：
 *  - 不在模块顶层 import `useToast`（Nuxt 自动导入 / PrimeVue composable），因为
 *    单测（bare vitest）环境没有该自动导入。改为由 `app.vue` 在 setup 中把真实的
 *    `useToast()` 结果通过 `registerToastApi` 注入进来。
 *  - 所有导出函数在“未注册”或 `import.meta.client === false` 时都是安全的空操作，
 *    绝不让 toast 逻辑破坏请求链路。
 *  - i18n 的 `t` 在非 Nuxt 上下文（含单测）下安全回退为原样返回 key，不抛错。
 */

/**
 * 仅测试用：显式覆盖 client 语义标志（生产代码勿调用）。
 * 背景：Vitest 的 import.meta 没有 Nuxt 的 client/server 语义（undefined → falsy），
 * 测试需显式注入。
 * 关键实现约束：生产运行时必须走**字面量** `import.meta.client`——Nuxt/Vite 的构建期
 * 静态替换只作用于该字面量表达式；若经 `const meta = import.meta` 别名访问
 * `meta.client`，别名属性在运行时不存在（undefined → 恒 falsy），客户端守卫会
 * 全部静默失效（2026-08 E2E 实测踩坑：toast 注册/弹写在浏览器全程空操作）。
 */
let clientFlagOverride: boolean | null = null;

export function _setClientFlag(client: boolean): void {
  clientFlagOverride = client;
}

/** 当前是否处于浏览器客户端环境（生产走构建期静态替换；测试走显式覆盖）。 */
function isClient(): boolean {
  if (clientFlagOverride !== null) return clientFlagOverride;
  return import.meta.client === true;
}

/** useToast() 返回的 ToastServiceMethods，我们只关心 .add(...)。 */
type ToastApi = Pick<ToastServiceMethods, 'add'>;

let toastApi: ToastApi | null = null;

/**
 * 注册全局 toast 实例。由 app.vue（客户端 setup）调用。
 * 非客户端或入参为空时不注册（保留空操作）。
 *
 * @param api useToast() 的返回值；传 null/undefined 表示注销（回到空操作）。
 */
export function registerToastApi(api: ToastApi | null): void {
  if (!isClient()) return;
  toastApi = api;
}

/**
 * 安全获取 i18n 翻译。
 *
 * 委托 `resolveRuntimeT()`（i18nRuntime.ts）在 Nuxt 运行时解析真正的翻译函数
 * （nuxt-i18n v10 的 `$i18n` 是不含 t 的 locale 状态代理，不能直接用）；
 * 单测 / 非 Nuxt 上下文回退为原样返回 key。无论哪种情况都不抛出。
 */
function safeT(key: string): string {
  if (!isClient()) return key;
  const t = resolveRuntimeT();
  return t ? t(key) : key;
}

/** 统一派发入口：未注册 / 非客户端时静默返回。 */
function show(message: ToastMessageOptions): void {
  if (!isClient() || !toastApi) return;
  toastApi.add(message);
}

/**
 * info 级别 toast。
 * @param summary 标题（已翻译）
 * @param detail  正文（可选）
 * @param life    展示时长（毫秒，默认 3000）
 */
export function toastInfo(summary?: string, detail?: string, life = 3000): void {
  show({ severity: 'info', summary, detail, life });
}

/**
 * success 级别 toast。
 * @param summary 标题（已翻译）
 * @param detail  正文（可选）
 * @param life    展示时长（毫秒，默认 3000）
 */
export function toastSuccess(summary?: string, detail?: string, life = 3000): void {
  show({ severity: 'success', summary, detail, life });
}

/**
 * warn 级别 toast。
 * @param summary 标题（已翻译）
 * @param detail  正文（可选）
 * @param life    展示时长（毫秒，默认 5000）
 */
export function toastWarn(summary?: string, detail?: string, life = 5000): void {
  show({ severity: 'warn', summary, detail, life });
}

/**
 * error 级别 toast。
 * @param summary 标题（已翻译）
 * @param detail  正文（可选）
 * @param life    展示时长（毫秒，默认 8000）
 */
export function toastError(summary?: string, detail?: string, life = 8000): void {
  show({ severity: 'error', summary, detail, life });
}

/** 请求失败兜底文案 key（与 locales/*.json 的 errors.requestFailed 对应）。 */
const REQUEST_FAILED_KEY = 'errors.requestFailed';

/**
 * 请求失败时统一弹出的 toast，供 requestApi.ts 在 fetch 失败后调用（保证每次请求
 * 至多一次）。summary 使用安全翻译的 `errors.requestFailed`。
 *
 * @param detail 额外的失败原因（可选）
 */
export function sendRequestErrorToast(detail?: string): void {
  const summary = safeT(REQUEST_FAILED_KEY);
  toastError(summary, detail);
}
