/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "@/components/**/*.{js,vue,ts}",
    "@/layouts/**/*.vue",
    "@/pages/**/*.vue",
    "@/plugins/**/*.{js,ts}",
    "@/nuxt.config.{js,ts}",
    "@/app.vue",
  ],
  theme: {
    extend: {
      width: {
        '15': '3.75rem', // 15 * 0.25rem = 3.75rem
        '13': '3.25rem', // 13 * 0.25rem = 3.25rem
        '17': '4.25rem', // 17 * 0.25rem = 4.25rem
        '18': '4.5rem', // 18 * 0.25rem = 4.5rem
        '19': '4.75rem', // 19 * 0.25rem = 4.75rem
      },
      height: {
        '15': '3.75rem', // 15 * 0.25rem = 3.75rem
        '13': '3.25rem', // 13 * 0.25rem = 3.25rem
        '17': '4.25rem', // 17 * 0.25rem = 4.25rem
        '18': '4.5rem', // 18 * 0.25rem = 4.5rem
        '19': '4.75rem', // 19 * 0.25rem = 4.75rem
      },
      zIndex: {
        '1':'1',
        '2':'2',
        '3':'3'
      }
    },
  },
  plugins: [],
}