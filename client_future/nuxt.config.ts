// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',

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
  modules: [
    '@nuxtjs/tailwindcss',
  ],

  tailwindcss: {
    cssPath: ['~/assets/css/tailwind.scss', { injectPosition: "first" }],
    configPath: '~/assets/ts/tailwind.config',
    exposeConfig: {
      level: 2
    },
    config: {},
    viewer: true,
  },

  ignore: ['**/src-tauri/**'],
  css:['~/assets/css/main.css', '~/assets/css/main.scss'],
})
