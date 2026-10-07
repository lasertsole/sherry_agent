/**
 * Toolbox bridge: the user terminal's two endpoints (see
 * `server/trigger/http/terminal.py`).
 *
 * The terminal runs ONE command per call in the session's project directory —
 * a console, not a PTY — and reports the process's own exit code, so a failing
 * command is data the panel renders rather than an error it toasts.
 *
 * @module bridge/toolbox
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol).

/** Where the terminal runs: the session's project directory. */
export interface TerminalInfo {
  cwd: string;
  shell: string;
}

/** One finished (or timed-out) command run. */
export interface TerminalRun {
  cwd: string;
  command: string;
  /** The process's exit code (``-1`` for a refused spawn or a timeout). */
  exit_code: number;
  /** stdout + stderr, folded into one stream, capped by the backend. */
  output: string;
  truncated: boolean;
  duration_ms: number;
}

/**
 * Read the directory the terminal runs in.
 * @param sessionId
 */
export async function fetchTerminalInfo(sessionId: string): Promise<TerminalInfo> {
  const res = await fetchApiPayload<Record<string, unknown>>({
    url: '/terminal/info',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return { cwd: String(res.cwd ?? ''), shell: String(res.shell ?? '') };
}

/**
 * Run one user-typed command in the session's project directory.
 * @param sessionId
 * @param command The command line (executed through a shell).
 */
export async function runTerminalCommand(sessionId: string, command: string): Promise<TerminalRun> {
  const res = await fetchApiPayload<Record<string, unknown>>({
    url: '/terminal/run',
    opts: { session_id: sessionId, command },
    method: 'post'
  });
  return {
    cwd: String(res.cwd ?? ''),
    command: String(res.command ?? command),
    exit_code: Number(res.exit_code ?? 0),
    output: String(res.output ?? ''),
    truncated: res.truncated === true,
    duration_ms: Number(res.duration_ms ?? 0)
  };
}
