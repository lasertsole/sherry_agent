/**
 * Channel settings bridge calls (`/channels` endpoints).
 *
 * @module bridge/channels
 */

/** A channel entry as returned by the backend `/channels` listing. */
export interface ChannelInfo {
  name: string;
  display_name: string;
  enabled: boolean;
  /** Persisted runtime toggle; gates scheduled heartbeat delivery (also needs a receiver). */
  heartbeat: boolean;
  /** Persisted runtime toggle for scheduled/cron behavior. */
  cron: boolean;
  icon: string;
}

/** Body for `updateChannel` — only the boolean toggles exposed by the settings UI. */
export interface ChannelUpdate {
  enabled?: boolean;
  heartbeat?: boolean;
  cron?: boolean;
}

/**
 * List all available channels.
 *
 * Mirrors `listSkills`: the timestamp query param is a legacy cache-buster
 * from the previous `useFetch`-based implementation and is now harmless
 * (repeated calls always return fresh channel status either way).
 */
export async function listChannels(): Promise<{ channels: ChannelInfo[] }> {
  return fetchApi({
    url: '/channels',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<{ channels: ChannelInfo[] }>;
}

/**
 * Persist per-channel runtime toggles (enabled/heartbeat/cron) to the backend.
 *
 * No Tauri IPC command exists for channels yet, so unlike write flows that
 * round-trip through Rust, channel writes go straight to the Python REST API
 * in both modes (mirrors `listChannels`).
 * @param channelName
 * @param update
 */
export async function updateChannel(channelName: string, update: ChannelUpdate): Promise<ChannelInfo> {
  return fetchApi({
    url: `/channels/${channelName}`,
    opts: { ...update },
    method: 'put'
  }) as unknown as Promise<ChannelInfo>;
}

/**
 * Free-form per-channel config dict, persisted to
 * plugins/channels/<name>/config.json (e.g. { app_id, receiver } for QQ).
 */
export type ChannelConfig = Record<string, unknown>;

/** Body/wrapper of `getChannelConfig`. */
export interface ChannelConfigResponse {
  channel_name: string;
  config: ChannelConfig;
}

/**
 * Read a channel's own config.json (plugins/channels/<name>/config.json),
 * returned as a free-form key/value map so the settings UI can render/edit
 * arbitrary fields (strings, numbers, booleans, lists).
 * @param channelName
 */
export async function getChannelConfig(channelName: string): Promise<ChannelConfigResponse> {
  return fetchApi({
    url: `/channels/${channelName}/config`,
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<ChannelConfigResponse>;
}

/**
 * Persist a channel's own config.json wholesale. The dict is stored verbatim,
 * preserving each value's JSON type. Mirrors the PUT semantics of the backend.
 * @param channelName
 * @param config
 */
export async function updateChannelConfig(channelName: string, config: ChannelConfig): Promise<ChannelConfigResponse> {
  return fetchApi({
    url: `/channels/${channelName}/config`,
    opts: { ...config },
    method: 'put'
  }) as unknown as Promise<ChannelConfigResponse>;
}
