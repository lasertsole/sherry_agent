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
  /** Hover tooltip (media entries carry none: the trigger names the category) */
  title?: string;
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
  /**
   * Backend tool-call id (only set when role=TOOL and the frame carried
   * `meta.tool_id`). This is the row's identity for pairing: one assistant
   * message may declare N tool calls, so the backend emits all N `tool_start`
   * frames first and only then their `tool_end`/`tool_result` frames — position
   * alone cannot tell them apart. Never set on history rows (the history API
   * does not expose it), so every lookup must tolerate its absence.
   */
  toolId?: string;
  /** Tool execution duration in ms (only set when role=TOOL; measured by the backend, monotonic) */
  toolDurationMs?: number;
  /** Earliest completion instant, epoch ms — freezes the running ticker (never on history rows) */
  toolEndedAtMs?: number;
  /** Tool status: running=calling, done=completed, failed=rejected/failed, error=execution error (only set when role=TOOL) */
  toolStatus?: 'running' | 'done' | 'failed' | 'error';
  /** Tool call arguments (only set when role=TOOL and a tool_result has been received) */
  toolArgs?: Record<string, unknown>;
  /** Tool execution result text (only set when role=TOOL and a tool_result has been received) */
  toolResult?: string;
  /** Model thinking/reasoning process (only set for role=AI; appended chunk by chunk when streaming, written in full at once when backfilling history) */
  reasoning?: string | null;
  /**
   * Still being streamed (only true for the live AI row of a running turn; never
   * set on history rows). The bubble uses it to render only the TAIL of a very
   * long answer while it streams — one enormous row starves the page — and the
   * full text once the turn settles.
   */
  streaming?: boolean;
  /** Model name (only set for role=AI; from the backend done frame / history row's model_name) */
  modelName?: string;
  /** Input token count (only set for role=AI; from the backend done frame / history row's input_tokens) */
  inputTokens?: number;
  /** Output token count (only set for role=AI; from the backend done frame / history row's output_tokens) */
  outputTokens?: number;
  /**
   * Origin marker (only on history rows whose backend origin is non-null; see
   * `CachedMessage.origin` in `composables/db.ts`). `"subagent_completion"` marks a
   * background-task completion carrier: a USER-role row that ChatBox renders as a centered,
   * muted system card instead of the regular user bubble. Legacy rows (no origin) keep the
   * existing user-bubble rendering.
   */
  origin?: string;
}

/** Role */
import { CHAT_ROLE } from '@/types/chat-role';

export { CHAT_ROLE };

/** HITL approval request (corresponds to the backend HitlInterruptData) */
export interface HitlRequestData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}
