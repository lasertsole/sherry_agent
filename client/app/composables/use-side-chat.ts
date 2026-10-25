/**
 * A side session: an ordinary session created from a quoted selection, derived
 * from its parent rather than started through the preset dialog. The server
 * seeds its first turn with the parent's checkpoint transcript (model-only) and
 * copies the parent's per-session settings.
 *
 * It is NOT a subagent: the user talks to it directly and it owns its own
 * checkpoint / history / approvals. It is PARENT-SCOPED, not tab-scoped:
 * closing its right-sidebar tab only detaches the socket, and the child stays
 * in the session list so it can be reopened. Only deleting the parent purges it
 * and every trace. What the client additionally copies is the parent's display
 * identity: the persona preset binding and the locked character snapshot. The
 * quoted selection is the first thing its composer carries.
 *
 * @module composables/use-side-chat
 */

// Bridge SUBmodule imports are explicit (not auto-imported from the facade) so
// this composable is test-mockable, same as use-chat-stream.

import type { CachedSessionMeta } from './db';
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
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { emit } from './mitt';
import { logUtil } from '~/utils/log';

/** What the caller needs to open the side-chat tab. */
export interface SideChatSession {
  /** The server-created child session id. */
  sessionId: string;
  /** The main session this side chat belongs to. */
  parentSessionId: string;
  /** Whether the server captured a parent transcript for model-only context. */
  inherited: boolean;
  /** The session-list placeholder (announced so the child is immediately visible). */
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
 * Create the child session's local display identity (preset binding and
 * character snapshot) and mirror the parent's agent config / project directory
 * onto the server. Every step is fail-open — a missing preset row or an
 * unreachable config endpoint degrades to the defaults, it never blocks the
 * side chat.
 *
 * The child is an ordinary session in the list (the panel can be closed and it
 * reopened later), so a placeholder meta is cached and announced at once. It
 * is replaced by the server-derived row on the next list refresh.
 *
 * @param parentSessionId The session the selection came from.
 * @param title The placeholder title shown in the session list.
 * @returns The new session id, its parent link and its placeholder meta.
 */
export async function createSideChatSession(parentSessionId: string, title: string): Promise<SideChatSession> {
  const created = await createSideChat(parentSessionId);
  const sessionId = created.sessionId;
  const meta: CachedSessionMeta = {
    id: sessionId,
    title,
    createTime: localCreateTime(new Date()),
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
  // The child is an ordinary session: announce its placeholder row so the list
  // shows it at once (loadSessionList replaces it with the server row later).
  emit('session:created', meta);

  return {
    sessionId,
    parentSessionId: created.parentSessionId,
    inherited: created.inherited,
    meta
  };
}

/**
 * Detach a side chat when its right-sidebar tab closes: release the socket and
 * leave the session, its checkpoint and every local cache untouched. The child
 * is destroyed only with its parent session.
 * @param sessionId Child session id.
 */
export function detachSideChatSession(sessionId: string): void {
  if (!sessionId) return;
  releaseAgentSocket(sessionId);
}

/**
 * Drop a side chat and every local cache row after its server-side data is
 * gone. Used by the parent-deletion cascade (the server has already purged the
 * child) and by an explicit discard.
 * @param sessionId Child session id.
 * @param parentSessionId Parent session id (delete-endpoint guard).
 * @param options Discard options.
 * @param options.serverAlreadyPurged Whether the parent cascade already removed
 *   the child and its link, so no delete request is needed.
 */
export async function discardSideChatSession(
  sessionId: string,
  parentSessionId?: string,
  options: { serverAlreadyPurged?: boolean } = {}
): Promise<void> {
  if (!sessionId) return;
  if (!options.serverAlreadyPurged) {
    await deleteSideChat(sessionId, parentSessionId);
  }
  releaseAgentSocket(sessionId);
  // A Dexie cleanup failure must not leave a dead tab behind.
  await Promise.allSettled([
    clearCachedCharacter(sessionId),
    clearCachedSessionPreset(sessionId),
    clearCachedSessionMeta(sessionId),
    clearSessionTitleOverride(sessionId)
  ]);
}
