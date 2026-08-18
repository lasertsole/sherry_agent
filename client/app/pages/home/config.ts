import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: 'toolbar.image',
    icon: 'pi pi-image',
    title: 'toolbar.uploadImage',
    event: 'uploadImage'
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
    toolName: 'toolbar.systemConfig',
    icon: 'pi pi-sliders-h',
    title: 'toolbar.systemConfig',
    event: 'systemConfig',
    label: 'toolbar.systemConfig'
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
