/**
 * 非 setup 上下文的 i18n 翻译函数解析器。
 *
 * 背景（2026-08 浏览器实测，两层坑）：
 *  1. @nuxtjs/i18n v10.6 会把 `nuxtApp.$i18n` / `globalProperties.$i18n` 替换为
 *     **仅含 locale 状态的代理**（无 `t`、无 `global`、`Object.keys` 为空），
 *     与官方文档「$i18n 即全局 Composer」相悖。
 *  2. 真正的 vue-i18n 实例挂在 vue-i18n 自己的注入键 `Symbol(vue-i18n)` 下；
 *     但本项目的 Vite 构建（@nuxtjs/i18n 内置 @intlify/unplugin-vue-i18n 对
 *     `'vue-i18n'` 的别名重定向）会把业务代码导入到**不含 `I18nInjectionKey`
 *     导出**的 dist 入口（运行时该值为 `undefined`，动态 import 实证），
 *     nuxt-i18n 插件与业务代码各持一份 vue-i18n 模块实例，符号同一性方案不可靠。
 *
 * 解析顺序：
 *  1. **形状扫描** `vueApp._context.provides`：查找形如 `{ global: { t: fn } }`
 *     的注入值（即 vue-i18n 实例）——不依赖符号/入口一致性，当前构建下唯一可靠路径
 *  2. `nuxtApp.$i18n.global.t`（旧版 nuxt-i18n / 官方文档行为，向后兼容）
 *  3. 都拿不到 → `undefined`（调用方回退为原样返回 key）
 *
 * 单测 / 非 Nuxt 上下文：`useNuxtApp` 不存在（ReferenceError）→ 被 try/catch
 * 吞掉返回 `undefined`，绝不抛出。setup 上下文请直接 `useI18n().t`
 * （见 errorCaptured.ts / useSubagentTasks.ts 的用法），勿用本函数。
 */

/** vue-i18n 全局 composer 的最小结构（本模块只关心 t）。 */
interface MinimalComposer {
  t: (key: string) => string;
}

/** vue-i18n 实例（createI18n 返回值）的最小结构：global 上挂全局 composer。 */
interface MinimalI18nInstance {
  global?: MinimalComposer;
}

/** 旧版 nuxt-i18n 形态：$i18n 直接带 global（v9 及文档描述的行为）。 */
interface MinimalNuxtI18n {
  global?: MinimalComposer;
}

/** 运行时翻译函数（只保证单 key 翻译语义）。 */
export type RuntimeT = (key: string) => string;

/**
 * 在 app 级 provides 中按形状查找 vue-i18n 全局 composer。
 *
 * 逐 key 读取并容忍 getter 抛错（provides 里可能有任意第三方注入值）。
 */
function findGlobalComposer(provides: Record<PropertyKey, unknown>): MinimalComposer | undefined {
  for (const key of Reflect.ownKeys(provides)) {
    let value: unknown;
    try {
      value = provides[key];
    } catch {
      continue;
    }
    if (!value || typeof value !== 'object') continue;
    const candidate = (value as MinimalI18nInstance).global;
    if (candidate && typeof candidate === 'object' && typeof candidate.t === 'function') {
      return candidate;
    }
  }
  return undefined;
}

/**
 * 解析可在**任意运行时上下文**（事件回调、定时器、非组件模块）使用的翻译函数。
 *
 * @returns 可用的 t 函数；Nuxt 不可用或 i18n 未挂载时返回 `undefined`。
 */
export function resolveRuntimeT(): RuntimeT | undefined {
  try {
    const nuxtApp = useNuxtApp();

    // 1) 形状扫描 provides（当前构建下唯一可靠的路径，见模块注释）
    const composer = findGlobalComposer(nuxtApp.vueApp._context.provides);
    if (composer) return (key) => composer.t(key);

    // 2) 旧版形态回退：$i18n.global.t（nuxt-i18n v9 及官方文档描述的行为）
    const $i18n = nuxtApp?.$i18n;
    const legacyT = ($i18n as MinimalNuxtI18n | undefined)?.global?.t;
    if (typeof legacyT === 'function') return (key) => legacyT(key);
  } catch {
    // 非 Nuxt 上下文（单测 / 纯函数调用）：回退为 undefined
  }
  return undefined;
}
