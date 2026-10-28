/**
 * MCP discovery composable: the snapshot fills the list, the drop-folder
 * actions degrade outside Tauri, and the restart routes through the bridge.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';

const bridge = vi.hoisted(() => ({
  fetchMcpServers: vi.fn(),
  restartBackendProcess: vi.fn()
}));
vi.mock('../bridge/mcp-servers', () => bridge);
vi.mock('../bridge/backend-process', () => bridge);

// The desktop-only file action lives in utils (Tauri opener); stub the module.
const fileActions = vi.hoisted(() => ({ openPathInFileManager: vi.fn() }));
vi.mock('~/utils/desktop-file-actions', () => fileActions);

import { openMcpFolder, refreshMcpServers, restartBackendForMcp, useMcpServers } from '../use-mcp-servers';

const snapshot = {
  servers: [
    { name: 'mine', source: 'user', command: 'npx', has_error: false, error: null },
    { name: 'broken', source: 'user', command: null, has_error: true, error: 'bad JSON' },
    { name: 'builtin', source: 'bundled', command: 'python', has_error: false, error: null }
  ],
  user_dir: '/home/user/mcp_servers'
};

describe('use-mcp-servers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.fetchMcpServers.mockResolvedValue(snapshot);
    bridge.restartBackendProcess.mockResolvedValue(true);
    fileActions.openPathInFileManager.mockResolvedValue(true);
  });

  it('refresh() fills the list and the drop-folder path', async () => {
    await refreshMcpServers();
    const { userServers, bundledServers, userDir, loaded } = useMcpServers();
    expect(userServers.value.map(s => s.name)).toEqual(['mine', 'broken']);
    expect(bundledServers.value.map(s => s.name)).toEqual(['builtin']);
    expect(userDir.value).toBe('/home/user/mcp_servers');
    expect(loaded.value).toBe(true);
  });

  it('a failed discovery keeps the previous snapshot and clears loading', async () => {
    await refreshMcpServers(); // first read fills the module-level state
    bridge.fetchMcpServers.mockRejectedValueOnce(new Error('offline'));
    await refreshMcpServers();
    const { loading, loaded, servers } = useMcpServers();
    expect(loading.value).toBe(false);
    expect(loaded.value).toBe(true, 'a failed read keeps what it had');
    expect(servers.value).toHaveLength(3);
  });

  it('openMcpFolder() aims at the discovered drop folder (desktop only)', async () => {
    await refreshMcpServers();
    expect(await openMcpFolder()).toBe(true);
    expect(fileActions.openPathInFileManager).toHaveBeenCalledWith('/home/user/mcp_servers');

    fileActions.openPathInFileManager.mockResolvedValue(false);
    const { userDir: empty } = useMcpServers();
    empty.value = '';
    expect(await openMcpFolder()).toBe(false);
  });

  it('restartBackendForMcp() routes through the bridge and confirms', async () => {
    const { restarting } = useMcpServers();
    await restartBackendForMcp();
    expect(bridge.restartBackendProcess).toHaveBeenCalledTimes(1);
    expect(restarting.value).toBe(false);
  });
});
