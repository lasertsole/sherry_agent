import { defineStore } from 'pinia';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/** One tool invocation that is executing right now. */
export interface RunningCommand {
  /** Message id of the TOOL row (stable identity for highlighting). */
  id: number;
  /** Tool that is running (terminal, python_repl, ...). */
  toolName: string;
  /** One-line summary of what it is doing (the command string when there is one). */
  summary: string;
  /** Start time in epoch ms (parsed from the row's compact timestamp), or null. */
  startedAtMs: number | null;
}

/**
 * Row timestamp → epoch ms.
 *
 * Persisted rows carry the backend's compact local-naive form
 * (``YYYYMMDDHHmmss``); rows created by the live stream carry an ISO string
 * (``new Date().toISOString()`` at ``tool_start``). Both must parse — the
 * elapsed column is what tells a running command from a stuck one.
 * @param timestamp
 */
function rowTimeToMs(timestamp: string): number | null {
  const value = timestamp ?? '';
  const match = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(value);
  if (match) {
    const [, y, mo, d, h, mi, s] = match;
    return new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s)).getTime();
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Best one-line description of a running tool call: the command / code it was
 * given when the arguments carry one, else its tool name.
 * @param message
 */
function summarize(message: MessageItem): string {
  const args = message.toolArgs ?? {};
  // `commands` is the terminal tool's own argument (string or list); the rest
  // cover the other executors (python_repl's `code`, the file tools' `path`).
  const candidate =
    args.commands ?? args.command ?? args.cmd ?? args.script ?? args.code ?? args.query ?? args.path ?? args.url;
  const text = Array.isArray(candidate) ? candidate.join(' && ') : candidate;
  if (typeof text === 'string' && text.trim()) {
    return text.trim().split('\n')[0]!.slice(0, 120);
  }
  const entries = Object.entries(args);
  if (entries.length) {
    const [key, value] = entries[0]!;
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    return `${key}: ${String(text).split('\n')[0]!.slice(0, 100)}`;
  }
  return message.toolName ?? '';
}

/**
 * Commands (tool calls) the session is running right now.
 *
 * The chat message list is the source of truth for in-flight tool calls: a TOOL
 * row with ``toolStatus === 'running'`` is executing, and the row's timestamp is
 * when it started. The session page syncs that list here through `sync()`, so
 * the toolbar's terminal entry reads one shared, reactive view.
 *
 * "Running" needs BOTH conditions, which is what keeps the list truthful: the
 * row is still marked running AND the session is generating. A tool row can
 * outlive its turn — a HITL pause holds it while the human decides, an aborted
 * stream or a lost ``tool_end`` leaves it behind — and a turn that is no longer
 * generating has nothing executing, so those rows must not be reported as
 * running commands.
 */
export const useRunningCommandsStore = defineStore('runningCommands', () => {
  const commands = ref<RunningCommand[]>([]);

  /**
   * Re-derive the running commands from the session's messages.
   * @param messages Current message list of the session on screen.
   * @param isSending Whether that session is generating right now (a finished
   *   turn owns no running commands, whatever the rows still say).
   */
  function sync(messages: MessageItem[] | undefined, isSending: boolean): void {
    commands.value = !isSending
      ? []
      : (messages ?? [])
          .filter(message => message.role === CHAT_ROLE.TOOL && message.toolStatus === 'running')
          .map(message => ({
            id: message.id,
            toolName: message.toolName ?? '',
            summary: summarize(message),
            startedAtMs: rowTimeToMs(message.timestamp)
          }));
  }

  return { commands, sync };
});
