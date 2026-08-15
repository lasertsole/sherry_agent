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
  /** 消息携带的图片。
   *  用户消息：原始 base64（不含 data: 前缀，需本地以 data:image/*;base64, 前缀渲染）；
   *  AI 消息：持久化后的绝对文件路径（如 C:/.../src/<session_id>/media/<ts>.png，
   *  需经后端 /media?session_id=<sid>&filename=<basename> 转成可访问 URL 渲染）。 */
  images?: string[];
  /** 消息id */
  id: number;
  /** 会话轮次 */
  turn_num: number;
  /** 时间戳 */
  timestamp: string;
  /** 工具名称（仅 role=TOOL 时有值，标识是哪个工具调用） */
  toolName?: string;
  /** 工具状态：running=调用中, done=已完成, failed=被拒绝/失败, error=执行出错（仅 role=TOOL 时有值） */
  toolStatus?: 'running' | 'done' | 'failed' | 'error';
  /** 工具调用参数（仅 role=TOOL 且收到 tool_result 时有值） */
  toolArgs?: Record<string, unknown>;
  /** 工具执行结果文本（仅 role=TOOL 且收到 tool_result 时有值） */
  toolResult?: string;
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

/** HITL 审批请求（对应后端 HitlInterruptData） */
export interface HitlRequestData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}
