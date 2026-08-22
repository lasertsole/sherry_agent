import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: 'toolbar.image',
    icon: 'pi pi-image',
    title: 'toolbar.uploadImage',
    event: 'uploadImage'
  },
  {
    toolName: 'toolbar.audio',
    icon: 'pi pi-microphone',
    title: 'toolbar.uploadAudio',
    event: 'uploadAudio'
  },
  {
    toolName: 'toolbar.video',
    icon: 'pi pi-video',
    title: 'toolbar.uploadVideo',
    event: 'uploadVideo'
  }
];

export const headerTools: Tool[] = [
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
    toolName: 'toolbar.systemConfig',
    icon: 'pi pi-sliders-h',
    title: 'toolbar.systemConfig',
    event: 'systemConfig',
    label: 'toolbar.systemConfig'
  },
  {
    toolName: 'toolbar.persona',
    icon: 'pi pi-user',
    title: 'toolbar.persona',
    event: 'persona',
    label: 'toolbar.persona'
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
    toolName: 'toolbar.extend',
    icon: 'puzzle-icon',
    title: 'toolbar.extend',
    event: 'extend',
    label: 'toolbar.extend'
  }
];

export default {};
