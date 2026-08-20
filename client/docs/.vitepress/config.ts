import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'EMA AI Agent - Backend API',
  description: 'Tauri IPC command reference for the EMA AI Agent backend',

  locales: {
    root: {
      label: 'English',
      lang: 'en',
    },
    zh: {
      label: '中文',
      lang: 'zh-CN',
      link: '/zh/',
      themeConfig: {
        nav: [
          { text: '指南', link: '/zh/guide/getting-started' },
          { text: '命令', link: '/zh/commands/agent' },
          { text: '事件', link: '/zh/events/streaming' },
          { text: '类型', link: '/zh/types/reference' },
        ],
        sidebar: [
          {
            text: '指南',
            items: [
              { text: '快速开始', link: '/zh/guide/getting-started' },
              { text: '错误处理', link: '/zh/guide/error-handling' },
            ],
          },
          {
            text: '命令',
            items: [
              { text: 'Agent', link: '/zh/commands/agent' },
              { text: 'Session', link: '/zh/commands/session' },
              { text: 'System Prompt', link: '/zh/commands/system-prompt' },
              { text: 'Character', link: '/zh/commands/character' },
              { text: 'System', link: '/zh/commands/system' },
            ],
          },
          {
            text: '实时通信',
            items: [
              { text: '流式事件', link: '/zh/events/streaming' },
            ],
          },
          {
            text: '参考',
            items: [
              { text: 'TypeScript 类型', link: '/zh/types/reference' },
              { text: '错误码', link: '/zh/guide/error-handling' },
            ],
          },
        ],
      },
    },
  },

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/guide/getting-started' },
      { text: 'Commands', link: '/commands/agent' },
      { text: 'Events', link: '/events/streaming' },
      { text: 'Types', link: '/types/reference' },
    ],

    sidebar: [
      {
        text: 'Guide',
        items: [
          { text: 'Getting Started', link: '/guide/getting-started' },
          { text: 'Error Handling', link: '/guide/error-handling' },
        ],
      },
      {
        text: 'Commands',
        items: [
          { text: 'Agent', link: '/commands/agent' },
          { text: 'Session', link: '/commands/session' },
          { text: 'System Prompt', link: '/commands/system-prompt' },
          { text: 'Character', link: '/commands/character' },
          { text: 'System', link: '/commands/system' },
        ],
      },
      {
        text: 'Real-time',
        items: [
          { text: 'Streaming Events', link: '/events/streaming' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'TypeScript Types', link: '/types/reference' },
          { text: 'Error Codes', link: '/guide/error-handling' },
        ],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/lasertsole/EMA_AI_agent' },
    ],

    search: {
      provider: 'local',
    },
  },
})
