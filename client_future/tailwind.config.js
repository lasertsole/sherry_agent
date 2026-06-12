/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class', // 主题切换
  content: [
    '@/components/**/*.{js,vue,ts}',
    '@/layouts/**/*.vue',
    '@/pages/**/*.vue',
    '@/plugins/**/*.{js,ts}',
    '@/nuxt.config.{js,ts}',
    '@/app.vue'
  ],
  theme: {
    screens: {
      sm: '480px',
      md: '768px',
      lg: '976px',
      xl: '1440px'
    },
    colors: {
      blue: '#1fb6ff',
      purple: '#7e5bef',
      pink: '#ff49db',
      orange: '#ff7849',
      green: '#13ce66',
      yellow: '#ffc82c',
      'gray-dark': '#273444',
      gray: '#8492a6',
      'gray-light': '#eef0f3'
    },
    fontFamily: {
      sans: ['Graphik', 'sans-serif'],
      serif: ['Merriweather', 'serif']
    },
    extend: {
      colors: {
        // 👈 映射自定义的文字颜色类名
        'theme-main': 'var(--theme-text)'
      },
      width: {
        15: '3.75rem', // 15 * 0.25rem = 3.75rem
        13: '3.25rem', // 13 * 0.25rem = 3.25rem
        17: '4.25rem', // 17 * 0.25rem = 4.25rem
        18: '4.5rem', // 18 * 0.25rem = 4.5rem
        19: '4.75rem' // 19 * 0.25rem = 4.75rem
      },
      height: {
        15: '3.75rem', // 15 * 0.25rem = 3.75rem
        13: '3.25rem', // 13 * 0.25rem = 3.25rem
        17: '4.25rem', // 17 * 0.25rem = 4.25rem
        18: '4.5rem', // 18 * 0.25rem = 4.5rem
        19: '4.75rem' // 19 * 0.25rem = 4.75rem
      },
      zIndex: {
        1: '1',
        2: '2',
        3: '3'
      }
    }
  },
  plugins: []
};
