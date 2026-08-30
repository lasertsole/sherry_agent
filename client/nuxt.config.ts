// https://nuxt.com/docs/api/configuration/nuxt-config
import tailwindcss from '@tailwindcss/vite';
import Aura from '@primevue/themes/aura';
import { definePreset } from '@primevue/themes';

// Custom dark theme preset
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
      link: [
          { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }
      ],
      meta: [
          { name: 'viewport', content: 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no' }
      ],
    }
  },

  // Do not use devtools
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

  // Import third-party modules
  modules: [
    '@nuxtjs/i18n',
    '@nuxtjs/color-mode',
    '@primevue/nuxt-module',
    '@pinia/nuxt',
    'pinia-plugin-persistedstate/nuxt'
  ],

  // pinia-plugin-persistedstate global config: persist everything uniformly to localStorage
  // (state is restored across browser refreshes and Tauri app restarts)
  piniaPluginPersistedstate: {
    storage: 'localStorage'
  },

  primevue: {
    options: {
      theme: {
        preset: NoirPreset,
        options: {
          // Configure the class name that triggers dark mode
          darkModeSelector: '.dark'
        }
      }
    }
  },

  i18n: {
    // Key point: the 'no_prefix' strategy. Based on this, Nuxt i18n sets __I18N_ROUTING__ to false at the
    // module level, thereby skipping the route-locale-detect middleware (it no longer force-overrides
    // composer.locale with the route-inferred locale on every navigation, and no longer rewrites URLs by
    // cookie). Before, with prefix_except_default, on every refresh/navigation the
    // loadAndSetLocale(detectLocale(route)) middleware would treat the prefix-less /home/:id as
    // defaultLocale(zh) and override the locale, causing "i18n to break after refresh"; moreover, when
    // cookie=en did not match the prefix-less path, it would redirect to /en/home/:id (a route that does
    // not exist). Under no_prefix the URL never carries a prefix and the language is fully delegated to
    // vue-i18n's locale (together with detectBrowserLanguage: false below + app.vue onMounted reading the
    // cookie itself to restore), so the language stays stable after refresh with no redirects.
    strategy: 'no_prefix',
    // Language matching priority: cookie (user preference) > browser language matched against valid
    // locales (zh/en/ja/ko) > English by default. Hence defaultLocale is set to 'en'.
    defaultLocale: 'en',
    langDir: '../app/i18n/locales',
    locales: [
      { code: 'zh', name: '简体中文', file: 'zh.json' },
      { code: 'en', name: 'English', file: 'en.json' },
      { code: 'ja', name: '日本語', file: 'ja.json' },
      { code: 'ko', name: '한국어', file: 'ko.json' }
    ],
    // Fully disable Nuxt i18n's browser language auto-detection/redirect. Combined with the 'no_prefix'
    // strategy, the language state is managed entirely by vue-i18n's locale, restored by app.vue
    // onMounted reading the i18n_locale cookie (or the browser language, fallback zh). This keeps the
    // language stable after refresh, the URL never prefixed, and no redirect ever rewrites the address.
    detectBrowserLanguage: false
  },

  ignore: ['**/src-tauri/**'],
  css:['~/assets/css/main.css', '~/assets/css/main.scss'],

  // Global directives
  vue: {
    compilerOptions: {
      // Enable directive transforms
      isCustomElement: (tag) => tag.startsWith('p-')
    }
  },

  // Auto import directives
  imports: {
    dirs: ['~/directives']
  },

  routeRules: {
    // Redirect to the home page by default
    '/': {
      redirect: {
        to: '/home',
        statusCode: 301
      },
    }
  }
})
