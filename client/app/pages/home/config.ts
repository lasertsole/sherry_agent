import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: 'toolbar.file',
    icon: 'pi pi-upload',
    title: 'toolbar.uploadFile',
    event: 'uploadFile'
  },
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
  }
];

export default {};
