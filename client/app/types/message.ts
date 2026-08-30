/** Tool call arguments */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  id: string;
  type: 'tool_call';
}

/** Usage metadata */
export interface UsageMetadata {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_token_details?: {
    cache_read?: number;
  };
  output_token_details?: Record<string, unknown>;
}

/** Response metadata */
export interface ResponseMetadata {
  finish_reason: string;
  model_name: string;
  system_fingerprint?: string;
  model_provider?: string;
}

/** Message data base class */
export interface MessageData {
  content: string;
  additional_kwargs: Record<string, unknown>;
}

/** Message base class */
export interface BaseMessage {
  type: string;
  data: MessageData;
  name: string | null;
  id: string;
}

/** AI message */
export interface AiMessage extends BaseMessage {
  type: 'ai';
  data: MessageData & { response_metadata: ResponseMetadata };
  tool_calls: ToolCall[];
  invalid_tool_calls: ToolCall[];
  usage_metadata: UsageMetadata | undefined;
}

/** Human message */
export interface HumanMessage extends BaseMessage {
  type: 'human';
}

/** Tool call result message */
export interface ToolMessage extends BaseMessage {
  type: 'tool';
  tool_call_id: string;
  /** Stack trace of the execution error */
  artifact: unknown;
  /** Status: 'success' | 'error' */
  status: string;
}

/** Multimodal message body (corresponds to Python type/__init__.py MultiModalMessage) */
export interface MultiModalMessage {
  /** Text content */
  text: string;
  /** Image path list */
  image_path_list?: string[];
  /** Image byte data (base64 string) */
  image_bytes_list?: string[];
  /** Image base64 list */
  image_base64_list?: string[];
  /** Audio path list */
  audio_path_list?: string[];
  /** Audio byte data (base64 string) */
  audio_bytes_list?: string[];
  /** Video path list */
  video_path_list?: string[];
  /** Video byte data (base64 string) */
  video_bytes_list?: string[];
}

/** Conversation message union type */
export type ConversationMessage = AiMessage | HumanMessage | ToolMessage;

/** Conversation history record */
export interface Conversation {
  id: string;
  messages: ConversationMessage[];
  created_at?: string;
  updated_at?: string;
}
