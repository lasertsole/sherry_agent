import type { CachedMessage } from '@/composables/db';
import { CHAT_ROLE, type MessageItem } from './type';

/**
 * Normalize backend-returned content to pure text string.
 *
 * Backend messages table's content exists in two forms:
 * 1. Multimodal structured array: `[{ type: 'text', text: '...' }, { type: 'image', ... }]`
 * 2. Pure text string: `'...'`
 *
 * ChatBox renders content through markdown-it, which only accepts strings (passing array throws
 * `Error: Input data should be a String`, causing the entire message list rendering to break).
 * Here we break down array form into pure text string (discard non-text segments, only concatenate text fields),
 * ensuring safe rendering and continuous content.
 *
 * (Extracted from `index/[sid].vue` as a pure module so the row→item mapping is unit-testable
 * without mounting the whole session page.)
 */
export const normalizeContent = (content: unknown): string => {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part: unknown) =>
        typeof part === 'object' && part !== null && typeof (part as { text?: unknown }).text === 'string'
          ? (part as { text: string }).text
          : ''
      )
      .join('');
  }
  // Any other shape (null / number / object etc.) falls back to an empty string
  return '';
};

/**
 * Convert backend-returned history message rows (CachedMessage[]) to MessageItem[] needed for chat list.
 * Structure matches backend messages table, only provides fallbacks for potentially empty fields, ensuring ChatBox rendering safety.
 *
 * Origin passthrough (subagent-origin-tagging): a row with `origin="subagent_completion"` is a
 * background-task completion carrier — the mapping copies it verbatim (`origin: row.origin ?? undefined`,
 * normalizing the backend's TEXT NULL / missing legacy key to `undefined`) so ChatBox can branch on
 * `role === USER && origin` and render a centered muted system card instead of a user bubble.
 */
export const toMessageItems = (rows: CachedMessage[]): MessageItem[] => {
  /**
   * Actual parameters (args) for tool calls don't exist on role=tool rows,
   * but are stored on the paired predecessor role=ai row (its tool_calls column is already persisted as JSON).
   *
   * So first scan all ai rows once, index { name, args } for each tool call id,
   * then precisely retrieve by tool_call_id on tool rows.
   *
   * Original form of tool_calls:
   *  - From the backend history API: already a parsed object array [ { id, name, args, type } ];
   *  - From local Dexie cache: might still be JSON string, so do safe parsing.
   */
  const toolCallById = new Map<string, { name?: string; args?: Record<string, unknown> }>();
  for (const row of rows) {
    if (row.role !== CHAT_ROLE.AI) continue;
    let calls: unknown = row.tool_calls;
    if (typeof calls === 'string') {
      try {
        calls = JSON.parse(calls);
      } catch {
        calls = null;
      }
    }
    if (!Array.isArray(calls)) continue;
    for (const call of calls) {
      if (typeof call !== 'object' || call === null) continue;
      const c = call as { id?: unknown; name?: unknown; args?: unknown };
      if (typeof c.id !== 'string' || !c.id) continue;
      toolCallById.set(c.id, {
        name: typeof c.name === 'string' ? c.name : undefined,
        args: typeof c.args === 'object' && c.args !== null ? (c.args as Record<string, unknown>) : undefined
      });
    }
  }

  return rows.map(row => {
    // role=tool rows: extract args from ai row index by tool_call_id, complete name and result
    if (row.role === CHAT_ROLE.TOOL && typeof row.tool_call_id === 'string') {
      const callInfo = toolCallById.get(row.tool_call_id);
      const rawStatus = row.tool_status ?? 'success';
      // Backend tool_status stores success/failed/error; the frontend display layer unifies them as done/failed/error
      const toolStatus: MessageItem['toolStatus'] =
        rawStatus === 'success' ? 'done' : (rawStatus as MessageItem['toolStatus']);
      return {
        session_id: row.session_id,
        role: CHAT_ROLE.TOOL,
        content: normalizeContent(row.content),
        images: row.images ?? undefined,
        id: row.id,
        turn_num: row.turn_num,
        timestamp: row.timestamp ?? '',
        // Name prefers the tool call name from the paired ai row (the tool_name column may also be missing)
        toolName: callInfo?.name ?? row.tool_name ?? undefined,
        toolStatus,
        // Retrieve real execution parameters from paired ai row
        toolArgs: callInfo?.args,
        toolResult: normalizeContent(row.content)
      };
    }

    return {
      session_id: row.session_id,
      role: row.role as CHAT_ROLE,
      content: normalizeContent(row.content),
      // Pass through image array: user messages are base64, AI messages are persisted file paths, ChatBox distinguishes rendering
      images: row.images ?? undefined,
      // Pass through audio/video array (same level as images: user messages are base64, AI messages are persisted file paths)
      audios: row.audios ?? undefined,
      videos: row.videos ?? undefined,
      id: row.id,
      turn_num: row.turn_num,
      timestamp: row.timestamp ?? '',
      // Pass through tool fields (role=tool rows in history messages will have values)
      toolName: row.tool_name ?? undefined,
      toolStatus: (row.tool_status as 'running' | 'done') ?? undefined,
      // Pass through model thinking/reasoning process (reasoning field of backend messages table, only on AI rows)
      reasoning: row.reasoning ?? null,
      // Pass through model metadata (model_name/input_tokens/output_tokens of backend messages table, only on AI rows)
      modelName: row.model_name ?? undefined,
      inputTokens: row.input_tokens ?? undefined,
      outputTokens: row.output_tokens ?? undefined,
      // Pass through the origin marker: "subagent_completion" = background-task carrier (TEXT NULL /
      // missing legacy key normalized to undefined = real user message). ChatBox branches on it.
      origin: row.origin ?? undefined
    };
  });
};
