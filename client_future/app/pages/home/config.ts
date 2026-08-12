import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: 'toolbar.newChat',
    icon: 'pi pi-comment',
    title: 'toolbar.newChat',
    event: 'createSession'
  },
  {
    toolName: 'toolbar.knowledgeBase',
    icon: 'pi pi-database',
    title: 'toolbar.knowledgeBase',
    event: 'knowledgeBase'
  },
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
    toolName: 'toolbar.configKnowledgeBase',
    icon: 'pi pi-file-edit',
    title: 'toolbar.configKnowledgeBase',
    event: 'knowledgeBase',
    label: 'toolbar.configKnowledgeBase'
  },
  {
    toolName: 'toolbar.userCenter',
    icon: 'pi pi-user',
    title: 'toolbar.userCenter',
    event: 'userCenter',
    label: 'toolbar.userCenter'
  }
];

export default {};
