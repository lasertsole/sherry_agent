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
  readCachedSessionPreset: vi.fn()
}));
const agentConfig = vi.hoisted(() => ({
  fetchAgentConfig: vi.fn(),
  setAgentConfig: vi.fn(async () => ({ config: {}, pending: false }))
}));
const projectSession = vi.hoisted(() => ({
  fetchProjectDirectory: vi.fn(),
  setProjectDirectory: vi.fn(async () => ({}))
}));
const bus = vi.hoisted(() => ({ emit: vi.fn() }));
const log = vi.hoisted(() => ({ e: vi.fn(), w: vi.fn(), i: vi.fn(), d: vi.fn() }));

vi.mock('../db', () => db);
vi.mock('../bridge/agent-config', () => agentConfig);
vi.mock('../bridge/session', () => projectSession);
vi.mock('../mitt', () => bus);
vi.mock('~/utils/log', () => ({ logUtil: log }));

import { createSideChatSession } from '../use-side-chat';

const parentCharacter = {
  session_id: 'parent',
  userName: 'U',
  userAvatar: '/u.png',
  aiName: 'A',
  aiAvatar: '/a.png'
};

beforeEach(() => {
  vi.clearAllMocks();
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
  it('copies the parent preset, character and agent config into the child', async () => {
    const { sessionId, meta } = await createSideChatSession('parent', '辅助会话');

    expect(sessionId).toMatch(/^[0-9a-f-]{36}$/);
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
    expect(db.cacheSessionMeta).toHaveBeenCalledWith(meta);
    expect(bus.emit).toHaveBeenCalledWith('session:created', meta);
  });

  it('still creates the session when the parent has no preset row', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    db.readCachedCharacter.mockResolvedValue(undefined);
    agentConfig.fetchAgentConfig.mockRejectedValue(new Error('offline'));

    const { sessionId } = await createSideChatSession('parent', '辅助会话');

    expect(sessionId).toBeTruthy();
    expect(db.cacheSessionPreset).not.toHaveBeenCalled();
    expect(db.cacheCharacter).not.toHaveBeenCalled();
    expect(db.cacheSessionMeta).toHaveBeenCalled();
    expect(log.w).toHaveBeenCalled();
  });
});
