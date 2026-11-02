/**
 * User-defined slash-command discovery (`GET /commands`).
 *
 * Commands are Markdown prompt templates under `APP_HOME/commands/` and the
 * active session's `<project>/.sherry/commands/`; the backend only reads and
 * normalizes them. The composer replaces `$ARGUMENTS` before sending.
 *
 * @module bridge/commands
 */

/** One custom command returned by the backend. */
export interface CustomCommand {
  /** `review` or `review:code` for a nested file. */
  name: string;
  description: string;
  /** Optional frontmatter `argument-hint` shown before insertion. */
  argument_hint: string;
  /** Prompt template; `$ARGUMENTS` is replaced by the user's argument. */
  body: string;
  /** `user` (APP_HOME/commands) or `workspace` (<project>/.sherry/commands). */
  source: 'user' | 'workspace';
}

/**
 * List the commands visible to one session.
 * @param sessionId Current session; omitted leaves workspace scope out.
 */
export async function listCommands(sessionId?: string): Promise<{ commands: CustomCommand[] }> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  return fetchApiPayload<{ commands: CustomCommand[] }>({
    url: `/commands${query}`,
    method: 'get'
  });
}
