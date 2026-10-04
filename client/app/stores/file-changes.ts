/**
 * File-change revert state for one session: what the agent wrote, and whether
 * it can still be put back.
 *
 * Fed by the `file_changes_updated` frame (pushed after every write) and by the
 * `file_changes_refresh` reply the store asks for on every socket open — the
 * push and the reply carry the identical payload, so there is one shape here.
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
// `on` / `emit` (the shared mitt bus), `fetchApiPayload` / `withGatewayToken`
// (the request transport) and `API_BASE_URL` are Nuxt auto-imports — imported
// explicitly they would trip the no-restricted-imports rule, exactly as they
// would in the taskflow store.
import { sessionIdFromPathname } from '../utils/session-route';

/** One tool call's file changes, as the backend groups them. */
export interface FileChangeGroup {
  tool_call_id: string;
  captured_at: number;
  paths: string[];
}

/** The payload shape (push frame content and refresh reply are identical). */
export interface FileChangesPayload {
  session_id: string;
  total_rows: number;
  changes: FileChangeGroup[];
  canRevert: boolean;
}

/** One file's line in a revert plan. */
export interface RevertFileResult {
  path: string;
  action: 'restore' | 'delete' | 'none';
  safe: boolean;
  reason?: string;
  ok?: boolean;
  detail?: string;
}

/** The revert response (applied or refused — both are answers). */
export interface RevertResult {
  success: boolean;
  error?: string;
  hint?: string;
  dry_run?: boolean;
  files: RevertFileResult[];
  merge3way?: {
    available: boolean;
    clean: boolean;
    conflict_paths: string[];
    command: string;
    note: string;
  } | null;
}

const EMPTY: FileChangesPayload = {
  session_id: '',
  total_rows: 0,
  changes: [],
  canRevert: false
};

export const useFileChangesStore = defineStore('file-changes', () => {
  const payload = ref<FileChangesPayload>({ ...EMPTY });
  const currentSid = ref('');
  const reverting = ref(false);
  const subscribed = ref(false);

  /** Number of files the session's writes touched (the chip's count). */
  const fileCount = computed(() => {
    const paths = new Set<string>();
    for (const change of payload.value.changes) for (const path of change.paths) paths.add(path);
    return paths.size;
  });

  const canRevert = computed(() => payload.value.canRevert && fileCount.value > 0);

  /** Resolve the session id from the route when the caller has none. */
  function resolveSid(): string {
    if (typeof window === 'undefined') return '';
    return sessionIdFromPathname(window.location.pathname) ?? '';
  }

  /**
   * Apply a payload (push frame or refresh reply).
   * @param incoming
   */
  function setPayload(incoming: unknown): void {
    const content = (incoming as { content?: FileChangesPayload } | null)?.content;
    if (!content || typeof content !== 'object') return;
    payload.value = {
      session_id: String(content.session_id ?? ''),
      total_rows: Number(content.total_rows ?? 0),
      changes: Array.isArray(content.changes) ? content.changes : [],
      canRevert: Boolean(content.canRevert)
    };
  }

  /** Register the mitt listeners once (singleton guard). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:file_changes_updated', setPayload);
    // `ws:connected` fires on every socket open, so a fresh page load recovers
    // the chip too, not just a reconnect.
    on('ws:connected', () => {
      refresh();
    });
  }

  /**
   * Ask the backend for the current state (outbound `ws:send` frame).
   * @param sid
   */
  function refresh(sid?: string): void {
    // `||`, not `??`: an empty string is "never resolved", not a session id.
    const target = sid || currentSid.value || resolveSid();
    currentSid.value = target ?? '';
    if (!currentSid.value) return;
    emit('ws:send', { event: 'file_changes_refresh', session_id: currentSid.value, content: '' });
  }

  /**
   * Plan (dry run) or apply a revert through the shared HTTP endpoint.
   * @param opts
   * @param opts.paths
   * @param opts.toToolCallId
   * @param opts.dryRun
   * @returns The payload, or null when the request itself failed.
   */
  async function revert(
    opts: {
      paths?: string[];
      toToolCallId?: string;
      dryRun?: boolean;
    } = {}
  ): Promise<RevertResult | null> {
    const sid = currentSid.value || resolveSid();
    if (!sid) return null;
    reverting.value = true;
    try {
      const result = await fetchApiPayload<RevertResult>({
        url: withGatewayToken(`${API_BASE_URL}/sessions/file-changes/revert`),
        method: 'post',
        opts: {
          session_id: sid,
          paths: opts.paths ?? [],
          to_tool_call_id: opts.toToolCallId ?? '',
          dry_run: Boolean(opts.dryRun)
        }
      });
      if (result && result.success && !opts.dryRun) refresh(sid);
      return result;
    } finally {
      reverting.value = false;
    }
  }

  return {
    payload,
    currentSid,
    reverting,
    subscribed,
    fileCount,
    canRevert,
    setPayload,
    subscribe,
    refresh,
    revert
  };
});
