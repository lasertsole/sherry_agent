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
const log = vi.hoisted(() => ({ e: vi.fn(), w: vi.fn(), i: vi.fn(), d: vi.fn() }));

vi.mock('../db', () => db);
vi.mock('../bridge/agent-config', () => agentConfig);
vi.mock('../bridge/session', () => projectSession);
vi.mock('../bridge/agent-socket', () => agentSocket);
vi.mock('~/utils/log', () => ({ logUtil: log }));

import { closeSideChatSession, createSideChatSession } from '../use-side-chat';

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
    const { sessionId, parentSessionId, inherited } = await createSideChatSession('parent');

    expect(projectSession.createSideChat).toHaveBeenCalledWith('parent');
    expect(sessionId).toBe('child-1');
    expect(parentSessionId).toBe('parent');
    expect(inherited).toBe(true);
    expect(db.cacheSessionPreset).toHaveBeenCalledWith(
      expect.objectContaining({ session_id: sessionId, preset_id: 'coding' })
    );
    expect(db.cacheCharacter).toHaveBeenCalledWith(expect.objectContaining({ session_id: sessionId, aiName: 'A' }));
    expect(agentConfig.setAgentConfig).toHaveBeenCalledWith(sessionId, {
      tools: ['read_file'],
      skills: []
    });
    expect(projectSession.setProjectDirectory).toHaveBeenCalledWith(sessionId, '/work/project');
    // Hidden from the normal session list: never a cached placeholder row.
    expect(db.cacheSessionMeta).not.toHaveBeenCalled();
  });

  it('still creates the session when the parent has no preset row', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    db.readCachedCharacter.mockResolvedValue(undefined);
    agentConfig.fetchAgentConfig.mockRejectedValue(new Error('offline'));

    const { sessionId } = await createSideChatSession('parent');

    expect(sessionId).toBe('child-1');
    expect(db.cacheSessionPreset).not.toHaveBeenCalled();
    expect(db.cacheCharacter).not.toHaveBeenCalled();
    expect(db.cacheSessionMeta).not.toHaveBeenCalled();
    expect(log.w).toHaveBeenCalled();
  });

  it('purges the child and clears local caches on close', async () => {
    await closeSideChatSession('child-1', 'parent');

    expect(projectSession.deleteSideChat).toHaveBeenCalledWith('child-1', 'parent');
    expect(agentSocket.releaseAgentSocket).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedCharacter).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedSessionPreset).toHaveBeenCalledWith('child-1');
    expect(db.clearCachedSessionMeta).toHaveBeenCalledWith('child-1');
    expect(db.clearSessionTitleOverride).toHaveBeenCalledWith('child-1');
  });
});
