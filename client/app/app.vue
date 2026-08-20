<template>
  <NuxtLayout>
    <!-- 顶层 <NuxtPage> 不再包裹 keepalive：per-session 状态由 home/index.vue 内层
         <NuxtPage :page-key="route.params.sid"> 的 KeepAlive 按 page-key 缓存。
         若两层同时 keepalive（嵌套 KeepAlive），切换 session 时外层按路由名缓存
         home/index 单槽、内层按 page-key 换子页，二者节奏不一致会在首帧产生
         「新旧子页 DOM 短暂共存」的错位残影。移除外层冗余 keepalive 消除该竞态。 -->
    <NuxtPage/>
  </NuxtLayout>
  <ImagePreviewOverlay />
</template>

<script lang="ts" setup>
// 客户端初始化恢复所选语言（配合 nuxt.config.ts 的 detectBrowserLanguage: false）。
// 背景：Nuxt i18n 的浏览器语言自动检测在 prefix_except_default 下会把 /home/:id 自动重定向
// 到 /en/home/:id（带前缀路由不存在）导致 i18n 失效，故已在配置里彻底关闭。此处由我们自行
// 接管：
//   1) 优先读持久化偏好 cookie（key: i18n_redirected，即 nuxt-i18n 模块 setLocale 写入的
//      默认 key）——用户上次选择的语言优先级最高；
//   2) 其次浏览器语言匹配合法 locale (zh/en/ja/ko)；
//   3) 匹配不中默认回退英文 en。
// 统一通过 setLocale() 生效：它既会加载该 locale 的语言包（避免首帧渲染原始 key），又会在
// 无偏好 cookie 时写入偏好 cookie，刷新后即可恢复语言且路由保持稳定。
const { setLocale } = useI18n();

// nuxt-i18n 在 detectBrowserLanguage: false 时默认使用的偏好 cookie key
const PREF_COOKIE = 'i18n_redirected';
const LOCALE_CODES = ['zh', 'en', 'ja', 'ko'] as const;
type LocaleCode = (typeof LOCALE_CODES)[number];

// 浏览器匹配不中时的默认回退语言（要求：不匹配系统语言时默认英文）
const DEFAULT_LOCALE = 'en' as const;

onMounted(async () => {
  if (!import.meta.client) return;

  // 1) cookie：用户上次选择的语言（优先级最高，持久化偏好）
  const cookieLocale = readCookie(PREF_COOKIE);
  if (cookieLocale && (LOCALE_CODES as readonly string[]).includes(cookieLocale)) {
    await setLocale(cookieLocale as LocaleCode);
    return;
  }

  // 2) 浏览器语言（初次访问），仅取前两位匹配到 zh/en/ja/ko 的
  const browserLang = navigator.language?.toLowerCase().slice(0, 2) ?? '';
  const matched = (LOCALE_CODES as readonly string[]).find((c) => c === browserLang);
  if (matched) {
    await setLocale(matched as LocaleCode);
    persistLocalePreference(matched as LocaleCode);
    return;
  }

  // 3) 浏览器语言不匹配任何 locale -> 回退默认英文
  await setLocale(DEFAULT_LOCALE);
  persistLocalePreference(DEFAULT_LOCALE);
});

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

/**
 * 手动写入语言偏好 cookie（key: i18n_redirected）。
 *
 * nuxt.config.ts 设 `detectBrowserLanguage: false` 后，模块把检测配置归一化为 `{}`，
 * 使 `setCookieLocale` 因 `detectConfig.useCookie` 为 falsy 而变成空操作——模块绝不写 cookie。
 * 因此 `setLocale` 只能即时切换，无法持久化。为满足「刷新/重开仍是偏好语言」，由我们手动
 * 写入偏好 cookie（与初载时的 readCookie 配合同套 key；与 home/index.vue 的
 * persistLocalePreference 行为一致）。
 */
function persistLocalePreference(code: LocaleCode) {
  if (!import.meta.client) return;
  // document.cookie 同步写入，同站路径，约一年有效期
  document.cookie = `${PREF_COOKIE}=${encodeURIComponent(code)}; path=/; max-age=31536000; SameSite=Lax`;
}
</script>

<style lang="scss" scoped>
  
</style>