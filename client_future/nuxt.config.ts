// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',

  app: {
    head: {
      meta: [
        { name: 'viewport', content: 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no' }
      ]
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
    server: {
      // Tauri requires a consistent port
      strictPort: true,
    },
  },

  // 导入第三方模块
  modules: ['@nuxtjs/tailwindcss', '@nuxtjs/i18n'],

  tailwindcss: {
    cssPath: ['~/assets/css/tailwind.scss', { injectPosition: "first" }],
    configPath: '~/assets/ts/tailwind.config',
    exposeConfig: {
      level: 2
    },
    config: {},
    viewer: true,
  },

  i18n: {
    strategy: 'prefix_except_default',
    defaultLocale: 'zh',
    langDir: new URL('./app/i18n/locales/', import.meta.url).pathname,
    locales: [
      { code: 'zh', name: '简体中文', file: 'zh.json' },
      { code: 'en', name: 'English', file: 'en.json' }
    ]
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
