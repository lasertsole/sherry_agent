// https://nuxt.com/docs/api/configuration/nuxt-config
import tailwindcss from '@tailwindcss/vite';
import Aura from '@primevue/themes/aura';
import { definePreset } from '@primevue/themes';

// 自定义黑色主题预设
const NoirPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '{slate.50}',
      100: '{slate.100}',
      200: '{slate.200}',
      300: '{slate.300}',
      400: '{slate.400}',
      500: '{slate.500}',
      600: '{slate.600}',
      700: '{slate.700}',
      800: '{slate.800}',
      900: '{slate.900}',
      950: '{slate.950}'
    }
  }
});

export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',

  app:{
    head:{
      title: process.env.VITE_APP_NAME,
      meta: [
          { name: 'viewport', content: 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no' }
      ],
    }
  },

  // 不使用开发工具
  devtools: { enabled: false },
  // Enable SSG
  ssr: false,

  // // Enables the development server to be discoverable by other devices when running on iOS physical devices
  // devServer: {
  //   host: '0',
  // },
  vite: {
    // Better support for Tauri CLI output
    clearScreen: false,
    // Enable environment variables
    // Additional environment variables can be found at
    // https://v2.tauri.app/reference/environment-variables/
    envPrefix: ['VITE_', 'TAURI_'],
    plugins: [tailwindcss()],
    server: {
      // Tauri requires a consistent port
      strictPort: true,
    },
  },

  // 导入第三方模块
  modules: ['@nuxtjs/i18n', '@nuxtjs/color-mode', '@primevue/nuxt-module'],

  primevue: {
    options: {
      theme: {
        preset: NoirPreset,
        options: {
          // 配置暗黑模式的触发类名
          darkModeSelector: '.dark'
        }
      }
    }
  },

  i18n: {
    // 关键：'no_prefix' 策略。Nuxt i18n 在 module 层据此把 __I18N_ROUTING__ 置为 false，
    // 从而跳过 route-locale-detect 中间件（不再在每个导航上用 route 推断的 locale 强制覆盖
    // composer.locale，也不再按 cookie 改写 URL）。在这之前用 prefix_except_default 时，
    // 每次刷新/导航中间件 loadAndSetLocale(detectLocale(route)) 会把无前缀的 /home/:id
    // 判定为 defaultLocale(zh) 并覆盖 locale，导致「刷新后国际化失效」；且 cookie=en 与
    // 无前缀路径不匹配时会重定向到 /en/home/:id（该路由不存在）。no_prefix 下 URL 永无前缀、
    // 语言完全交给 vue-i18n 的 locale（配合下方 detectBrowserLanguage: false + app.vue
    // onMounted 自行读 cookie 恢复），刷新后语言稳定且不跳转。
    strategy: 'no_prefix',
    // 语言匹配优先级：cookie(用户偏好) > 浏览器语言匹配合法 locale
    // (zh/en/ja/ko) > 默认英文。因此 defaultLocale 设为 'en'。
    defaultLocale: 'en',
    langDir: '../app/i18n/locales',
    locales: [
      { code: 'zh', name: '简体中文', file: 'zh.json' },
      { code: 'en', name: 'English', file: 'en.json' },
      { code: 'ja', name: '日本語', file: 'ja.json' },
      { code: 'ko', name: '한국어', file: 'ko.json' }
    ],
    // 彻底关闭 Nuxt i18n 的浏览器语言自动检测/重定向。结合 'no_prefix' 策略，语言状态完全由
    // vue-i18n 的 locale 管理，由 app.vue onMounted 读取 i18n_locale cookie（或浏览器语言，
    // fallback zh）自行恢复。这样刷新后语言稳定、URL 永无前缀、绝不重定向改写地址。
    detectBrowserLanguage: false
  },

  ignore: ['**/src-tauri/**'],
  css:['~/assets/css/main.css', '~/assets/css/main.scss'],

  routeRules: {
    // 默认重定向至home页
    '/': {
      redirect: {
        to: '/home',
        statusCode: 301
      },
    }
  }
})
