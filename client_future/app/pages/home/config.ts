import type { Tool } from './type';

export const tools: Tool[] = [
  {
    toolName: '新建对话',
    icon: '',
    title: '新建对话',
    event: 'createSession'
  },
  {
    toolName: '知识库',
    icon: '',
    title: '知识库',
    event: 'knowledgeBase'
  },
  {
    toolName: '文件',
    icon: '',
    title: '上传文件',
    event: 'uploadFile'
  },
  {
    toolName: '图片',
    icon: '',
    title: '上传图片',
    event: 'uploadImage'
  }
];

export const headerTools: Tool[] = [
  {
    toolName: '配置知识库',
    icon: '',
    title: '配置知识库',
    event: 'knowledgeBase'
  },
  {
    toolName: '个人中心',
    icon: '',
    title: '个人中心',
    event: 'userCenter'
  },
  {
    toolName: '主题模式',
    icon: '',
    title: '主题模式',
    event: 'changeTheme'
  }
];

export default {};
