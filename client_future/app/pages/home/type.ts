/** 会话纪录 */
export interface SessionRecord {
  /** 标题 */
  title: string;
  /** 创建时间 */
  createTime: string;
  /** id */
  id: string;
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
}
