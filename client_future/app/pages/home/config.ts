import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: '新建对话',
    icon: 'pi pi-comment',
    title: '新建对话',
    event: 'createSession'
  },
  {
    toolName: '知识库',
    icon: 'pi pi-database',
    title: '知识库',
    event: 'knowledgeBase'
  },
  {
    toolName: '文件',
    icon: 'pi pi-upload',
    title: '上传文件',
    event: 'uploadFile'
  },
  {
    toolName: '图片',
    icon: 'pi pi-image',
    title: '上传图片',
    event: 'uploadImage'
  }
];

export const headerTools: Tool[] = [
  {
    toolName: '配置知识库',
    icon: 'pi pi-file-edit',
    title: '配置知识库',
    event: 'knowledgeBase'
  },
  {
    toolName: '个人中心',
    icon: 'pi pi-user',
    title: '个人中心',
    event: 'userCenter'
  },
  {
    toolName: '主题模式',
    icon: 'pi pi-sliders-h',
    title: '主题模式',
    event: 'changeTheme'
  }
];

export default {};
