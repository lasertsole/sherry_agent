/**
 * Auto-skill curator control bridge calls (`/curator` endpoints).
 *
 * @module bridge/curator
 */

/** Auto-transition counters returned by `run_curator_review` (e.g. marked_stale/archived/reactivated/checked/seeded). */
export interface CuratorAutoTransitions {
  marked_stale: number;
  archived: number;
  reactivated: number;
  checked: number;
  seeded: number;
}

/** Mirrors the result object returned by `context_engine/curator/orchestrator.run_curator_review`. */
export interface CuratorRunResult {
  started_at: string;
  auto_transitions: CuratorAutoTransitions;
  /** Human-readable run summary (e.g. "no changes"). */
  summary_so_far: string;
  /** Present when the LLM consolidation pass failed (e.g. model not configured). */
  error?: string;
}

/** Response of `POST /curator/run`. */
export interface CuratorRunResponse {
  success: boolean;
  result?: CuratorRunResult;
  error?: string;
}

/** Settings returned by `GET /curator/settings`. */
export interface CuratorSettings {
  success: boolean;
  /** Auto-maintenance interval override in days, null when unset (falls back to `interval_hours`). */
  auto_interval_days: number | null;
  /** Configured maintenance interval in hours (from the sherry.jsonc curator settings). */
  interval_hours: number;
  /** ISO timestamp of the last curator run, null when never run. */
  last_run_at: string | null;
  /** ISO timestamp of the last maintenance, null when never run. */
  last_maintenance_at: string | null;
  error?: string;
}

/** Response of `PUT /curator/settings`. */
export interface CuratorSettingsUpdateResponse {
  success: boolean;
  /** The stored override (null = back to the sherry.jsonc curator default). */
  auto_interval_days: number | null;
  /** Effective interval in hours (override days x 24, or the sherry.jsonc curator default). */
  interval_hours: number;
  /** ISO timestamp of the last maintenance, null when never run. */
  last_maintenance_at: string | null;
  error?: string;
}

/**
 * Force-trigger a curator review/maintenance run against the auto-learned
 * skills. Calls `run_curator_review` on the backend (the forced entry point,
 * not the idle-scheduled `maybe_run_curator`), so it always executes.
 *
 * @returns `{ success, result, error }` from the backend.
 */
export async function runCuratorReview(): Promise<CuratorRunResponse> {
  return fetchApi({ url: '/curator/run', method: 'post' }) as unknown as Promise<CuratorRunResponse>;
}

/**
 * Read the curator auto-maintenance settings.
 *
 * @returns `{ success, auto_interval_days, interval_hours, last_run_at, last_maintenance_at }`.
 */
export async function getCuratorSettings(): Promise<CuratorSettings> {
  return fetchApi({ url: '/curator/settings', method: 'get' }) as unknown as Promise<CuratorSettings>;
}

/**
 * Override the curator auto-maintenance interval.
 *
 * @param days Days between auto-maintenance runs (1-5), or null to use the sherry.jsonc curator default.
 * @returns `{ success, auto_interval_days, interval_hours, last_maintenance_at }`.
 */
export async function setCuratorSettings(days: number | null): Promise<CuratorSettingsUpdateResponse> {
  return fetchApi({
    url: '/curator/settings',
    opts: { auto_interval_days: days },
    method: 'put'
  }) as unknown as Promise<CuratorSettingsUpdateResponse>;
}
