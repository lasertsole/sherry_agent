/** Session record */
export interface SessionRecord {
  /** Title */
  title: string;
  /** Creation time */
  createTime: string;
  /** id */
  id: string;
  /** Messages */
  messages?: MessageItem[];
  /** Whether the user has renamed it via editing: after editing, the title no longer follows the
   *  last user message and is displayed in a highlight color */
  renamed?: boolean;
}

/** Toolbar tool */
export interface Tool {
  /** Tool name */
  toolName: string;
  /** Icon */
  icon: string;
  /** Hover tooltip */
  title: string;
  /** Event to trigger */
  event: string;
  /** label--for component adaptation */
  label?: string;
}

/** Message */
export interface MessageItem {
  /** Session id */
  session_id: string;
  /** Role */
  role: CHAT_ROLE;
  /** Content */
  content: string;
  /** Images carried by the message.
   *  User message: raw base64 (without the data: prefix; must be rendered locally with a data:image/*;base64, prefix);
   *  AI message: persisted absolute file path (e.g. C:/.../src/<session_id>/media/<ts>.png,
   *  which must go through the backend /media?session_id=<sid>&filename=<basename> to become a renderable URL). */
  images?: string[];
  /** Audios carried by the message.
   *  User message: raw base64 (without the data: prefix; must be rendered locally with a data:audio/*;base64, prefix);
   *  AI message: persisted absolute file path (e.g. C:/.../src/<session_id>/media/<ts>.mp3,
   *  which must go through the backend /media?session_id=<sid>&filename=<basename> to become a renderable URL). */
  audios?: string[];
  /** Videos carried by the message.
   *  User message: raw base64 (without the data: prefix; must be rendered locally with a data:video/*;base64, prefix);
   *  AI message: persisted absolute file path (e.g. C:/.../src/<session_id>/media/<ts>.mp4,
   *  which must go through the backend /media?session_id=<sid>&filename=<basename> to become a renderable URL). */
  videos?: string[];
  /** Message id */
  id: number;
  /** Conversation turn number */
  turn_num: number;
  /** Timestamp */
  timestamp: string;
  /** Tool name (only set when role=TOOL; identifies which tool call) */
  toolName?: string;
  /** Tool status: running=calling, done=completed, failed=rejected/failed, error=execution error (only set when role=TOOL) */
  toolStatus?: 'running' | 'done' | 'failed' | 'error';
  /** Tool call arguments (only set when role=TOOL and a tool_result has been received) */
  toolArgs?: Record<string, unknown>;
  /** Tool execution result text (only set when role=TOOL and a tool_result has been received) */
  toolResult?: string;
  /** Model thinking/reasoning process (only set for role=AI; appended chunk by chunk when streaming, written in full at once when backfilling history) */
  reasoning?: string | null;
  /** Model name (only set for role=AI; from the backend done frame / history row's model_name) */
  modelName?: string;
  /** Input token count (only set for role=AI; from the backend done frame / history row's input_tokens) */
  inputTokens?: number;
  /** Output token count (only set for role=AI; from the backend done frame / history row's output_tokens) */
  outputTokens?: number;
}

/** Role */
export enum CHAT_ROLE {
  /** ai */
  AI = 'ai',
  /** Tool */
  TOOL = 'tool',
  /** User */
  USER = 'human'
}

/** HITL approval request (corresponds to the backend HitlInterruptData) */
export interface HitlRequestData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}
