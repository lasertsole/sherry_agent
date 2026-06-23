/** 会话纪录 */
export interface SessionRecord {
  /** 标题 */
  title: string;
  /** 创建时间 */
  createTime: string;
  /** id */
  id: string;
  /** 消息 */
  messages?: MessageItem[];
}

/** 工具栏工具 */
export interface Tool {
  /** 工具名称 */
  toolName: string;
  /** 图标 */
  icon: string;
  /** hover提示 */
  title: string;
  /** 触发事件提示 */
  event: string;
  /** label--适配组件 */
  label?: string;
}

/** 消息 */
export interface MessageItem {
  /** 会话id */
  session_id: string;
  /** 角色 */
  role: CHAT_ROLE;
  /** 内容 */
  content: string;
  /** 消息id */
  id: number;
  /** 会话轮次 */
  turn_num: number;
  /** 时间戳 */
  timestamp: string;
}

/** 角色 */
export enum CHAT_ROLE {
  /** ai */
  AI = 'ai',
  /** 工具 */
  TOOL = 'tool',
  /** 用户 */
  USER = 'human'
}
