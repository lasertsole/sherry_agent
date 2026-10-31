/**
 * MCP server discovery bridge (`GET /mcp/servers`).
 *
 * The loader merges the read-only bundled defaults with the user drop folder
 * (`APP_HOME/mcp_servers/` — flat `<name>.json` or self-contained
 * `<name>/config.json`). The listing is pure discovery: nothing is spawned,
 * nothing connects, and a bad JSON file shows as a ⚠ entry instead of an
 * error — a config change takes effect on the next backend restart.
 *
 * @module bridge/mcp-servers
 */
import { fetchApiPayload } from '../requestApi';

/** One discovered MCP server, as the list view renders it. */
export interface McpServerEntry {
  /** The server's name (the file stem / directory name). */
  name: string;
  /** `bundled` (shipped defaults) or `user` (the drop folder). */
  source: 'bundled' | 'user';
  /** The spec's command, when the entry parsed. */
  command: string | null;
  /** True when the JSON could not be parsed (the loader skips it). */
  has_error: boolean;
  /** The parse error, when `has_error`. */
  error: string | null;
}

/** The wire shape of `GET /mcp/servers`. */
export interface McpServersSnapshot {
  servers: McpServerEntry[];
  /** The absolute drop-folder path (the "open folder" button's target). */
  user_dir: string;
}

/**
 * Discover the MCP servers the loader would pick up.
 * @returns The server list and the drop folder's absolute path.
 */
export async function fetchMcpServers(): Promise<McpServersSnapshot> {
  const res = await fetchApiPayload<{
    success?: boolean;
    servers?: McpServerEntry[];
    user_dir?: string;
  }>({ url: '/mcp/servers', method: 'get' });
  return {
    servers: Array.isArray(res.servers) ? res.servers : [],
    user_dir: typeof res.user_dir === 'string' ? res.user_dir : ''
  };
}
