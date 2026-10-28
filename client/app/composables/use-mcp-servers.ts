/**
 * MCP server discovery state (the extend panel's MCP tab).
 *
 * Mirrors `GET /mcp/servers` (pure discovery: the loader's merged view of the
 * bundled defaults + the user drop folder, parsed per entry so one bad file
 * shows as a ⚠ entry). A config change takes effect on the next backend
 * restart, so the panel pairs "open the drop folder" with a restart action —
 * the same strategy the CUDA pack uses.
 *
 * Outside the Tauri runtime the desktop-only actions resolve to false and the
 * restart button hides; the list itself works everywhere (the web build talks
 * to the same backend).
 */

import { computed, onMounted, ref } from 'vue';

import type { McpServerEntry } from './bridge/mcp-servers';
// Stable module specifiers so tests can vi.mock the bridge; the unimport
// injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchMcpServers } from './bridge/mcp-servers';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { restartBackendProcess } from './bridge/backend-process';
import { openPathInFileManager } from '~/utils/desktop-file-actions';
import { isTauriRuntime } from '~/utils/project-directory';
import { logUtil } from '~/utils/log';

const servers = ref<McpServerEntry[]>([]);
const userDir = ref('');
const loading = ref(false);
const loaded = ref(false);
const restarting = ref(false);
const restartHint = ref(false);

/** Whether the restart affordance exists (a shell can actually restart). */
const canRestart = ref(false);

/** Bundled entries and user entries, listed separately in the panel. */
const userServers = computed(() => servers.value.filter(server => server.source === 'user'));
const bundledServers = computed(() => servers.value.filter(server => server.source === 'bundled'));

/** Re-read the snapshot from the backend. */
export async function refreshMcpServers(): Promise<void> {
  loading.value = true;
  try {
    const snapshot = await fetchMcpServers();
    servers.value = snapshot.servers;
    userDir.value = snapshot.user_dir;
    loaded.value = true;
  } catch (e) {
    logUtil.e('[McpServers] discovery failed:', e);
  } finally {
    loading.value = false;
  }
}

/** Open the drop folder in the OS file manager (desktop only). */
export async function openMcpFolder(): Promise<boolean> {
  if (!userDir.value) return false;
  return openPathInFileManager(userDir.value);
}

/** Restart the backend so freshly dropped configs take effect. */
export async function restartBackendForMcp(): Promise<void> {
  restarting.value = true;
  try {
    await restartBackendProcess();
    restartHint.value = false;
  } catch (e) {
    logUtil.e('[McpServers] backend restart failed:', e);
  } finally {
    restarting.value = false;
  }
}

/** Bind the state into a component (the tab's setup hook). */
export function useMcpServers() {
  onMounted(async () => {
    canRestart.value = isTauriRuntime();
    void refreshMcpServers();
  });
  return {
    servers,
    userServers,
    bundledServers,
    userDir,
    loading,
    loaded,
    restarting,
    restartHint,
    canRestart,
    refreshMcpServers,
    openMcpFolder,
    restartBackendForMcp
  };
}
