/** 工具调用参数 */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  id: string;
  type: 'tool_call';
}

/** 使用量元数据 */
export interface UsageMetadata {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_token_details?: {
    cache_read?: number;
  };
  output_token_details?: Record<string, unknown>;
}

/** 响应元数据 */
export interface ResponseMetadata {
  finish_reason: string;
  model_name: string;
  system_fingerprint?: string;
  model_provider?: string;
}

/** 消息数据基类 */
export interface MessageData {
  content: string;
  additional_kwargs: Record<string, unknown>;
}

/** 消息基类 */
export interface BaseMessage {
  type: string;
  data: MessageData;
  name: string | null;
  id: string;
}

/** AI 消息 */
export interface AiMessage extends BaseMessage {
  type: 'ai';
  data: MessageData & { response_metadata: ResponseMetadata };
  tool_calls: ToolCall[];
  invalid_tool_calls: ToolCall[];
  usage_metadata: UsageMetadata | undefined;
}

/** 人类消息 */
export interface HumanMessage extends BaseMessage {
  type: 'human';
}

/** 工具调用结果消息 */
export interface ToolMessage extends BaseMessage {
  type: 'tool';
  tool_call_id: string;
  /** 执行错误的堆栈信息 */
  artifact: unknown;
  /** 状态: 'success' | 'error' */
  status: string;
}

/** 多模态消息体（对应 Python type/__init__.py MultiModalMessage） */
export interface MultiModalMessage {
  /** 文本内容 */
  text: string;
  /** 图片路径列表 */
  image_path_list?: string[];
  /** 图片字节数据（base64 字符串） */
  image_bytes_list?: string[];
  /** 图片 base64 列表 */
  image_base64_list?: string[];
  /** 音频路径列表 */
  audio_path_list?: string[];
  /** 音频字节数据（base64 字符串） */
  audio_bytes_list?: string[];
  /** 视频路径列表 */
  video_path_list?: string[];
  /** 视频字节数据（base64 字符串） */
  video_bytes_list?: string[];
}

/** 对话消息联合类型 */
export type ConversationMessage = AiMessage | HumanMessage | ToolMessage;

/** 对话历史记录 */
export interface Conversation {
  id: string;
  messages: ConversationMessage[];
  created_at?: string;
  updated_at?: string;
}
