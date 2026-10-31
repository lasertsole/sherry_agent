/**
 * `.env` contract constants shared by the environment-config UI and its model
 * panels.
 *
 * The 200K floor is enforced in three places, all reading these values so the
 * hint, the client-side pre-check, and the backend stay in step:
 *  - the inline hint above each guarded input (`LlmModelManager.vue`);
 *  - the pre-PUT guard in `ConfigPanel.vue::persistEnvChanges` (the right-sidebar
 *    tab that edits the `other` group);
 *  - the backend's own `write_env_file` guard
 *    (`server/service/env.py::TOKEN_KEYS` / `MIN_REQUIRED_MAX_TOKEN`), which is
 *    authoritative — a save that slips past the client is still refused.
 *
 * @module constants/env
 */

/**
 * Keys whose context window must stay at or above {@link MIN_REQUIRED_MAX_TOKEN}.
 *
 * The two REQUIRED families plus the two optional internal families
 * (TaskFlow decisions / compression) are guarded; the other media-model
 * budgets are intentionally left free.
 */
export const MAX_TOKEN_GUARD_KEYS = [
  'MAIN_LLM_MAX_TOKEN',
  'AUXILIARY_LLM_MAX_TOKEN',
  'TASKFLOW_LLM_MAX_TOKEN',
  'COMPRESSION_LLM_MAX_TOKEN'
] as const;

/** Smallest accepted context window (tokens) for the guarded keys. */
export const MIN_REQUIRED_MAX_TOKEN = 200000;
