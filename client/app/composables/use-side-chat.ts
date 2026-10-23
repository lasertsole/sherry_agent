/**
 * A side session: an ordinary session created from a quoted selection, derived
 * from its parent rather than started through the preset dialog.
 *
 * It is NOT a subagent: the user talks to it directly, it owns its own
 * checkpoint / history / approvals, and it appears in the session list like any
 * other session. What it inherits is only the parent's display and behaviour
 * identity: the persona preset binding, the locked character snapshot, the
 * agent config (tools / middlewares / skills / per-role models) and the main
 * model override. The conversation itself starts empty; the quoted selection is
 * the first thing its composer carries.
 *
 * @module composables/use-side-chat
 */

import type { CachedSessionMeta } from './db';
// Bridge SUBmodule imports are explicit (not auto-imported from the facade) so
// this composable is test-mockable, same as use-chat-stream.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchAgentConfig, setAgentConfig } from './bridge/agent-config';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchProjectDirectory, setProjectDirectory } from './bridge/session';
import { logUtil } from '~/utils/log';

/** What the caller needs to open the side-chat tab. */
export interface SideChatSession {
  /** The newly minted session id. */
  sessionId: string;
  /** The placeholder meta row (already cached and announced to the sidebar). */
  meta: CachedSessionMeta;
}

/**
 * `YYYY-MM-DD HH:mm` — the format the session list compares lexicographically.
 * @param date
 * @returns The formatted local timestamp.
 */
function localCreateTime(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

/**
 * Create the child session's local identity: id, preset binding, character
 * snapshot and agent config. Every step is fail-open — a missing preset row or
 * an unreachable config endpoint degrades to the defaults, it never blocks the
 * side chat.
 *
 * @param parentSessionId The session the selection came from.
 * @param title The placeholder title shown in the session list.
 * @returns The new session id and its cached placeholder meta.
 */
export async function createSideChatSession(parentSessionId: string, title: string): Promise<SideChatSession> {
  const sessionId = crypto.randomUUID();
  const now = new Date();
  const meta: CachedSessionMeta = {
    id: sessionId,
    title,
    createTime: localCreateTime(now),
    updatedAt: Date.now()
  };

  // Preset binding: display metadata only (the persona itself is already in the
  // workspace files this session's prompt will read).
  try {
    const binding = await readCachedSessionPreset(parentSessionId);
    if (binding) {
      await cacheSessionPreset({ ...binding, session_id: sessionId });
    }
  } catch (e) {
    logUtil.w('[SideChat] preset binding copy failed:', e);
  }

  // Character snapshot: copy the parent's locked names/avatars, falling back to
  // the global pending profile when the parent never locked one.
  try {
    const character = (await readCachedCharacter(parentSessionId)) ?? (await readCachedCharacter(GLOBAL_SESSION_KEY));
    if (character) {
      await cacheCharacter({ ...character, session_id: sessionId });
    }
  } catch (e) {
    logUtil.w('[SideChat] character snapshot copy failed:', e);
  }

  // Agent config: same tool face / middleware switches / skills / role models
  // as the parent. An older parent with no explicit config reads as "{}", which
  // is exactly the default the child would run with anyway.
  try {
    const parent = await fetchAgentConfig(parentSessionId);
    if (parent.config && Object.keys(parent.config).length > 0) {
      await setAgentConfig(sessionId, parent.config);
    }
  } catch (e) {
    logUtil.w('[SideChat] agent config copy failed:', e);
  }

  // Project directory: the side chat must work in the SAME root as the session
  // it was quoted from — otherwise it would resolve against the process default
  // and read an unrelated project's AGENTS.md. An unbound parent stays unbound.
  try {
    const project = await fetchProjectDirectory(parentSessionId);
    if (project.directory) {
      await setProjectDirectory(sessionId, project.directory);
    }
  } catch (e) {
    logUtil.w('[SideChat] project directory copy failed:', e);
  }

  await cacheSessionMeta(meta).catch(e => {
    logUtil.e('[SideChat] session meta cache failed:', e);
  });
  // The left sidebar keeps its own list; announce the row so it appears now
  // (the server list picks it up after the first persisted turn).
  emit('session:created', meta);

  return { sessionId, meta };
}
