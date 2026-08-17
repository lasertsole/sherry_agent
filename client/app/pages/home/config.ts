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
    toolName: 'toolbar.systemConfig',
    icon: 'pi pi-sliders-h',
    title: 'toolbar.systemConfig',
    event: 'systemConfig',
    label: 'toolbar.systemConfig'
  },
  {
    toolName: 'toolbar.extend',
    icon: 'pi pi-th-large',
    title: 'toolbar.extend',
    event: 'extend',
    label: 'toolbar.extend'
  },
  {
    toolName: 'toolbar.logs',
    icon: 'pi pi-terminal',
    title: 'toolbar.logs',
    event: 'logs',
    label: 'toolbar.logs'
  }
];

export default {};
