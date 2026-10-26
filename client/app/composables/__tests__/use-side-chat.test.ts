/**
 * Side-session creation: an ordinary child session derived from its parent.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const db = vi.hoisted(() => ({
  GLOBAL_SESSION_KEY: '__global__',
  cacheCharacter: vi.fn(async () => undefined),
  cacheSessionMeta: vi.fn(async () => undefined),
  cacheSessionPreset: vi.fn(async () => undefined),
  readCachedCharacter: vi.fn(),
  readCachedSessionPreset: vi.fn(),
  clearCachedCharacter: vi.fn(async () => undefined),
  clearCachedSessionMeta: vi.fn(async () => undefined),
  clearCachedSessionPreset: vi.fn(async () => undefined),
  clearSessionTitleOverride: vi.fn(async () => undefined)
}));
const agentConfig = vi.hoisted(() => ({
  fetchAgentConfig: vi.fn(),
  setAgentConfig: vi.fn(async () => ({ config: {}, pending: false }))
}));
const projectSession = vi.hoisted(() => ({
  createSideChat: vi.fn(),
  deleteSideChat: vi.fn(async () => undefined),
  fetchProjectDirectory: vi.fn(),
  setProjectDirectory: vi.fn(async () => ({}))
}));
const agentSocket = vi.hoisted(() => ({ releaseAgentSocket: vi.fn() }));
const mitt = vi.hoisted(() => ({ emit: vi.fn() }));
const log = vi.hoisted(() => ({ e: vi.fn(), w: vi.fn(), i: vi.fn(), d: vi.fn() }));

vi.mock('../db', () => db);
vi.mock('../bridge/agent-config', () => agentConfig);
vi.mock('../bridge/session', () => projectSession);
vi.mock('../bridge/agent-socket', () => agentSocket);
vi.mock('../mitt', () => mitt);
vi.mock('~/utils/log', () => ({ logUtil: log }));

import { createSideChatSession, detachSideChatSession, discardSideChatSession } from '../use-side-chat';

const parentCharacter = {
  session_id: 'parent',
  userName: 'U',
  userAvatar: '/u.png',
  aiName: 'A',
  aiAvatar: '/a.png'
};

beforeEach(() => {
  vi.clearAllMocks();
  projectSession.createSideChat.mockResolvedValue({
    sessionId: 'child-1',
    parentSessionId: 'parent',
    inherited: true
  });
  db.readCachedSessionPreset.mockResolvedValue({
    session_id: 'parent',
    preset_id: 'coding',
    preset_name: '编程助手'
  });
  db.readCachedCharacter.mockResolvedValue(parentCharacter);
  agentConfig.fetchAgentConfig.mockResolvedValue({
    config: { tools: ['read_file'], skills: [] },
    pending: false
  });
  projectSession.fetchProjectDirectory.mockResolvedValue({
    directory: '/work/project',
    effective: '/work/project',
    source: 'session',
    pending: null
  });
});

describe('createSideChatSession', () => {
  it('copies the parent preset, character and agent config into the server-created child', async () => {
    const { sessionId, parentSessionId, inherited, meta } = await createSideChatSession('parent', '辅助会话');

    expect(projectSession.createSideChat).toHaveBeenCalledWith('parent', expect.any(String));
    expect(sessionId).toBe('child-1');
    expect(parentSessionId).toBe('parent');
    expect(inherited).toBe(true);
    expect(meta.id).toBe(sessionId);
    expect(meta.title).toBe('辅助会话');
    expect(db.cacheSessionPreset).toHaveBeenCalledWith(
      expect.objectContaining({ session_id: sessionId, preset_id: 'coding' })
    );
    expect(db.cacheCharacter).toHaveBeenCalledWith(expect.objectContaining({ session_id: sessionId, aiName: 'A' }));
    expect(agentConfig.setAgentConfig).toHaveBeenCalledWith(sessionId, {
      tools: ['read_file'],
      skills: []
    });
    expect(projectSession.setProjectDirectory).toHaveBeenCalledWith(sessionId, '/work/project');
    // The child is an ordinary list row: cache it and announce it immediately.
    expect(db.cacheSessionMeta).toHaveBeenCalledWith(meta);
    expect(mitt.emit).toHaveBeenCalledWith('session:created', meta);
  });

  it('still creates the session when the parent has no preset row', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    db.readCachedCharacter.mockResolvedValue(undefined);
    agentConfig.fetchAgentConfig.mockRejectedValue(new Error('offline'));

    const { sessionId } = await createSideChatSession('parent', '辅助会话');

    expect(sessionId).toBe('child-1');
    expect(db.cacheSessionPreset).not.toHaveBeenCalled();
    expect(db.cacheCharacter).not.toHaveBeenCalled();
    expect(db.cacheSessionMeta).toHaveBeenCalled();
    expect(mitt.emit).toHaveBeenCalled();
    expect(log.w).toHaveBeenCalled();
  });

  it('detaches the panel without deleting the child or its caches', () => {
    detachSideChatSession('child-1');

    expect(projectSession.deleteSideChat).not.toHaveBeenCalled();
    expect(agentSocket.releaseAgentSocket).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedCharacter).not.toHaveBeenCalled();
    expect(db.clearCachedSessionPreset).not.toHaveBeenCalled();
    expect(db.clearCachedSessionMeta).not.toHaveBeenCalled();
    expect(db.clearSessionTitleOverride).not.toHaveBeenCalled();
  });

  it('discards local caches only after an explicit server purge', async () => {
    await discardSideChatSession('child-1', 'parent');

    expect(projectSession.deleteSideChat).toHaveBeenCalledWith('child-1', 'parent');
    expect(agentSocket.releaseAgentSocket).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedCharacter).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedSessionPreset).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedSessionMeta).toHaveBeenCalledWith('child-1');
    expect(db.clearSessionTitleOverride).toHaveBeenCalledWith('child-1');
  });

  it('skips the delete request when the parent cascade already purged the child', async () => {
    await discardSideChatSession('child-1', 'parent', { serverAlreadyPurged: true });

    expect(projectSession.deleteSideChat).not.toHaveBeenCalled();
    expect(db.clearCachedCharacter).toHaveBeenCalledWith('child-1');
  });

  it('reuses the idempotency key for immediate re-creations on the same parent', async () => {
    // A lost response followed by a re-click must name the SAME child — the
    // server answers the first fork instead of building a second complete one.
    await createSideChatSession('parent-idem-a', '辅助会话');
    await createSideChatSession('parent-idem-a', '辅助会话');

    const calls = projectSession.createSideChat.mock.calls.filter(c => c[0] === 'parent-idem-a');
    expect(calls).toHaveLength(2);
    expect(calls[0][1]).toBeTruthy();
    expect(calls[1][1]).toBe(calls[0][1]);

    // A different parent in the same window is a different, deliberate fork.
    await createSideChatSession('parent-idem-b', '辅助会话');
    const other = projectSession.createSideChat.mock.calls.find(c => c[0] === 'parent-idem-b');
    expect(other?.[1]).toBeTruthy();
    expect(other?.[1]).not.toBe(calls[0][1]);
  });
});
