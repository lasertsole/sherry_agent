/**
 * Cron (scheduled tasks) bridge calls (`/cron` endpoints).
 *
 * @module bridge/cron
 */

/** Schedule of a cron job (camelCase JSON shape matching `CronSchedule`). */
export interface CronSchedule {
  kind: 'at' | 'every' | 'cron';
  /** Absolute epoch millis — required when kind === 'at'. */
  atMs?: number | null;
  /** Interval in millis — required when kind === 'every'. */
  everyMs?: number | null;
  /** Cron expression — required when kind === 'cron'. */
  expr?: string | null;
  /** IANA timezone name, optional. */
  tz?: string | null;
}

/** Payload of a cron job (camelCase JSON shape matching `CronPayload`). */
export interface CronPayload {
  kind: string;
  /** The message the job sends when it fires. */
  message: string;
  /** Whether the message should be delivered to a channel. */
  deliver: boolean;
  /** Channel id used when deliver is true. */
  channel?: string | null;
  /** Optional recipient override (e.g. chat_id / user id). */
  to?: string | null;
}

/** Runtime state of a cron job (camelCase JSON shape matching `CronJobState`). */
export interface CronJobState {
  nextRunAtMs?: number | null;
  lastRunAtMs?: number | null;
  lastStatus?: string | null;
  lastError?: string | null;
}

/** A single cron job as returned by the backend `/cron` endpoints. */
export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  schedule: CronSchedule;
  payload: CronPayload;
  state: CronJobState;
  createdAtMs?: number | null;
  updatedAtMs?: number | null;
  deleteAfterRun: boolean;
}

/** Response of `GET /cron`. */
export interface CronListResponse {
  jobs: CronJob[];
  success?: boolean;
}

/** Response of `POST /cron`, `PUT /cron`, and `POST /cron/enable`. */
export interface CronMutateResponse {
  success: boolean;
  job?: CronJob;
  message?: string;
}

/** Response of `POST /cron/trigger` and `DELETE /cron`. */
export interface CronActionResponse {
  success: boolean;
  message?: string;
}

/**
 * List cron jobs.
 *
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless (repeated
 * calls after add/edit/run always return fresh job state regardless).
 *
 * @param includeDisabled Whether to include jobs whose `enabled` flag is false.
 */
export async function listCronJobs(includeDisabled = false): Promise<CronListResponse> {
  const resp = (await fetchApi({
    url: '/cron',
    opts: { _ts: Date.now(), include_disabled: includeDisabled },
    method: 'get'
  })) as unknown as CronListResponse;
  return resp;
}

/**
 * Create a new cron job.
 *
 * @param input The job fields (mirrors the `POST /cron` body).
 * @param input.name Job display name.
 * @param input.message Task message sent to the agent on each run.
 * @param input.schedule Cron expression / interval schedule.
 * @param input.deliver Whether the run result is delivered to a channel.
 * @param input.channel Target channel id when `deliver` is set.
 * @param input.to Channel recipient (e.g. QQ number) when `deliver` is set.
 * @param input.delete_after_run Whether the job removes itself after its next run.
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function addCronJob(input: {
  name: string;
  message: string;
  schedule: CronSchedule;
  deliver?: boolean;
  channel?: string | null;
  to?: string | null;
  delete_after_run?: boolean;
}): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron',
    opts: input,
    method: 'post'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Update an existing cron job.
 *
 * @param id   The job id to update.
 * @param patch Partial fields to merge onto the existing job.
 * @param patch.name Job display name.
 * @param patch.message Task message sent to the agent on each run.
 * @param patch.schedule Cron expression / interval schedule.
 * @param patch.deliver Whether the run result is delivered to a channel.
 * @param patch.channel Target channel id when `deliver` is set.
 * @param patch.to Channel recipient (e.g. QQ number) when `deliver` is set.
 * @param patch.delete_after_run Whether the job removes itself after its next run.
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function updateCronJob(
  id: string,
  patch: {
    name?: string;
    message?: string;
    schedule?: CronSchedule;
    deliver?: boolean;
    channel?: string | null;
    to?: string | null;
    delete_after_run?: boolean;
  }
): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron',
    opts: { id, ...patch },
    method: 'put'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Manually trigger a cron job now.
 *
 * @param id    The job id to run.
 * @param force When false (default) a disabled job is skipped.
 * @returns `{ success, message? }` from the backend.
 */
export async function runCronJob(id: string, force = false): Promise<CronActionResponse> {
  return fetchApi({
    url: '/cron/trigger',
    opts: { id, force },
    method: 'post'
  }) as unknown as Promise<CronActionResponse>;
}

/**
 * Enable or disable a cron job.
 *
 * @param id      The job id.
 * @param enabled Desired activation state (default true).
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function enableCronJob(id: string, enabled = true): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron/enable',
    opts: { id, enabled },
    method: 'post'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Remove a cron job. This is irreversible — callers MUST confirm first.
 *
 * @param id The job id to delete.
 * @returns `{ success, message? }` from the backend.
 */
export async function deleteCronJob(id: string): Promise<CronActionResponse> {
  return fetchApi({
    url: '/cron',
    opts: { id },
    method: 'delete'
  }) as unknown as Promise<CronActionResponse>;
}
