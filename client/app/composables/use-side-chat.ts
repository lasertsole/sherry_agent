/**
 * A side session: an ordinary session created from a quoted selection, derived
 * from its parent rather than started through the preset dialog. The server
 * seeds its first turn with the parent's checkpoint transcript (model-only) and
 * copies the parent's per-session settings.
 *
 * It is NOT a subagent: the user talks to it directly and it owns its own
 * checkpoint / history / approvals. It is EPHEMERAL and hidden from the normal
 * session list: closing its right-sidebar tab (or deleting the parent) purges
 * it and every trace. What the client additionally copies is the parent's
 * display identity: the persona preset binding and the locked character
 * snapshot. The quoted selection is the first thing its composer carries.
 *
 * @module composables/use-side-chat
 */

// Bridge SUBmodule imports are explicit (not auto-imported from the facade) so
// this composable is test-mockable, same as use-chat-stream.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchAgentConfig, setAgentConfig } from './bridge/agent-config';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { createSideChat, deleteSideChat, fetchProjectDirectory, setProjectDirectory } from './bridge/session';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  clearCachedCharacter,
  clearCachedSessionMeta,
  clearCachedSessionPreset,
  clearSessionTitleOverride
} from './db';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { releaseAgentSocket } from './bridge/agent-socket';
import { logUtil } from '~/utils/log';

/** What the caller needs to open the side-chat tab. */
export interface SideChatSession {
  /** The server-created child session id. */
  sessionId: string;
  /** The main session this side chat belongs to. */
  parentSessionId: string;
  /** Whether the server captured a parent transcript for model-only context. */
  inherited: boolean;
}

/**
 * Create the child session's local display identity (preset binding and
 * character snapshot) and mirror the parent's agent config / project directory
 * onto the server. Every step is fail-open — a missing preset row or an
 * unreachable config endpoint degrades to the defaults, it never blocks the
 * side chat.
 *
 * No session-list placeholder is cached: a side chat is hidden from the normal
 * list for its whole life.
 *
 * @param parentSessionId The session the selection came from.
 * @returns The new session id and its parent link.
 */
export async function createSideChatSession(parentSessionId: string): Promise<SideChatSession> {
  const created = await createSideChat(parentSessionId);
  const sessionId = created.sessionId;

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

  return {
    sessionId,
    parentSessionId: created.parentSessionId,
    inherited: created.inherited
  };
}

/**
 * Close an ephemeral side chat: optionally purge it server-side, release its
 * socket and drop every local cache row. Callers close the tab afterwards.
 * @param sessionId Child session id.
 * @param parentSessionId Parent session id (purge guard).
 * @param options Close options.
 * @param options.purge Whether to call the delete endpoint (false when the
 *   server is already cascading the delete from the parent).
 */
export async function closeSideChatSession(
  sessionId: string,
  parentSessionId?: string,
  options: { purge?: boolean } = {}
): Promise<void> {
  if (!sessionId) return;
  if (options.purge !== false) {
    await deleteSideChat(sessionId, parentSessionId);
  }
  releaseAgentSocket(sessionId);
  // The server purge already succeeded; a Dexie cleanup failure must not leave
  // a dead tab behind.
  await Promise.allSettled([
    clearCachedCharacter(sessionId),
    clearCachedSessionPreset(sessionId),
    clearCachedSessionMeta(sessionId),
    clearSessionTitleOverride(sessionId)
  ]);
}
