import type { Tool } from './type';

/**
 * Media entries of the session input toolbar: the option list behind the plus
 * glyph (see home/components/MediaMenu.vue).
 */
export const tools: Tool[] = [
  {
    toolName: 'toolbar.image',
    icon: 'pi pi-image',
    event: 'uploadImage'
  },
  {
    toolName: 'toolbar.audio',
    icon: 'pi pi-microphone',
    event: 'uploadAudio'
  },
  {
    toolName: 'toolbar.video',
    icon: 'pi pi-video',
    event: 'uploadVideo'
  }
];

/**
 * The toolbox dialog's entries (the hammer button): the browser and the user's
 * terminal. Deliberately NOT a part of `headerTools` — that grid is the settings
 * menu, and these two are working surfaces.
 */
export const toolboxTools: Tool[] = [
  {
    toolName: 'toolbar.browser',
    icon: 'pi pi-desktop',
    title: 'toolbar.browser',
    event: 'browser',
    label: 'toolbar.browser'
  },
  {
    toolName: 'toolbar.terminal',
    icon: 'pi pi-server',
    title: 'toolbar.terminal',
    event: 'terminal',
    label: 'toolbar.terminal'
  },
  {
    toolName: 'toolbar.whiteboard',
    icon: 'pi pi-pen-to-square',
    title: 'toolbar.whiteboard',
    event: 'whiteboard',
    label: 'toolbar.whiteboard'
  }
];

export const headerTools: Tool[] = [
  // FIRST: System Config. It holds the app-wide settings (interface language
  // included), so it is the entry a user reaches for before anything else.
  {
    toolName: 'toolbar.systemConfig',
    icon: 'pi pi-sliders-h',
    title: 'toolbar.systemConfig',
    event: 'systemConfig',
    label: 'toolbar.systemConfig'
  },
  // SECOND: the preset panel is where a session's identity is set up, so it
  // follows immediately (its own name is Presets & Role).
  {
    toolName: 'toolbar.persona',
    icon: 'pi pi-user',
    title: 'toolbar.persona',
    event: 'persona',
    label: 'toolbar.persona'
  },
  {
    toolName: 'toolbar.skills',
    icon: 'pi pi-bolt',
    title: 'toolbar.skills',
    event: 'skills',
    label: 'toolbar.skills'
  },
  {
    toolName: 'toolbar.knowledgeGraph',
    icon: 'pi pi-sitemap',
    title: 'toolbar.knowledgeGraph',
    event: 'knowledgeGraph',
    label: 'toolbar.knowledgeGraph'
  },
  {
    toolName: 'toolbar.stats',
    icon: 'pi pi-chart-bar',
    title: 'toolbar.stats',
    event: 'stats',
    label: 'toolbar.stats'
  },
  {
    toolName: 'toolbar.logs',
    icon: 'pi pi-history',
    title: 'toolbar.logs',
    event: 'logs',
    label: 'toolbar.logs'
  },
  {
    toolName: 'toolbar.account',
    icon: 'pi pi-id-card',
    title: 'toolbar.account',
    event: 'account',
    label: 'toolbar.account'
  },
  // The search-backend panel (Tavily / SearXNG): its own concern next to the
  // model and tool editors, so it keeps its own menu entry.
  {
    toolName: 'toolbar.webSearch',
    icon: 'pi pi-globe',
    title: 'toolbar.webSearch',
    event: 'webSearch',
    label: 'toolbar.webSearch'
  },
  {
    toolName: 'toolbar.memory',
    icon: 'pi pi-database',
    title: 'toolbar.memory',
    event: 'memory',
    label: 'toolbar.memory'
  },
  {
    toolName: 'toolbar.heartbeat',
    icon: 'pi pi-heart',
    title: 'toolbar.heartbeat',
    event: 'heartbeat',
    label: 'toolbar.heartbeat'
  },
  {
    toolName: 'toolbar.cron',
    icon: 'pi pi-clock',
    title: 'toolbar.cron',
    event: 'cron',
    label: 'toolbar.cron'
  },
  {
    toolName: 'toolbar.extend',
    icon: 'puzzle-icon',
    title: 'toolbar.extend',
    event: 'extend',
    label: 'toolbar.extend'
  }
];
