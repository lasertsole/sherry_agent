/**
 * Skill management bridge calls (`/skills` endpoints).
 *
 * @module bridge/skills
 */

/** A skill entry as returned by the backend `/skills` listing. */
export interface SkillInfo {
  name: string;
  description: string;
  location: string;
  category: 'builtin' | 'auto' | 'third_party';
  /** Whether the skill is excluded from curator maintenance (never merged/removed). */
  pinned?: boolean;
}

/** A single node in a skill's on-disk directory structure (relative to the skill root). */
export interface SkillFileNode {
  /** Relative path from the skill root, e.g. `scripts/core.py` or `references/part01.md`. */
  path: string;
  /** Basename of the file/directory, e.g. `core.py`. */
  name: string;
  /** `file` for regular files, `dir` for directories. */
  type: 'file' | 'dir';
  /** UTF-8 text content — present only on `file` nodes. */
  content?: string;
}

export interface SkillDetail extends SkillInfo {
  content: string;
  /** Recursively-ordered directory structure under the skill's folder (SKILL.md + references/scripts/etc.). */
  files?: SkillFileNode[];
}

/** Response of `POST /skills/delete`. */
export interface DeleteSkillResponse {
  success: boolean;
  name?: string;
  message?: string;
}

/** Response of `POST /skills/pin`. */
export interface PinSkillResponse {
  success: boolean;
  name?: string;
  pinned?: boolean;
  message?: string;
}

/**
 * List all skills (builtin, auto, third_party).
 *
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless (repeated
 * calls after a curator run or any lifecycle change always return fresh data).
 */
export async function listSkills(): Promise<{ skills: SkillInfo[] }> {
  return fetchApi({
    url: '/skills',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<{ skills: SkillInfo[] }>;
}

/**
 * Read a single skill's full SKILL.md content.
 * @param location
 */
export async function readSkill(location: string): Promise<SkillDetail> {
  const cleanPath = location.replace(/^\.\//, '');
  return fetchApi({ url: `/skills/${cleanPath}`, method: 'get' }) as unknown as Promise<SkillDetail>;
}

/**
 * Upload a third-party skill from a local SKILL.md file.
 *
 * Sends the file as `multipart/form-data` (field `file`) plus an optional
 * `name` field derived from the file's base name without extension. The
 * uploaded skill is inactive by default.
 *
 * @param file The local SKILL.md file to upload.
 * @returns `{ success, message?, name?, warnings? }` from the backend. When
 *   `warnings` is non-empty the upload was accepted (CAUTION scan verdict) but
 *   the security scanner flagged concerns that should be surfaced to the user.
 */
export async function uploadSkill(
  file: File
): Promise<{ success: boolean; message?: string; name?: string; warnings?: string[] }> {
  const formData = new FormData();
  formData.append('file', file);
  const baseName = file.name.replace(/\.[^.]+$/, '');
  formData.append('name', baseName);
  return fetchApi({
    url: '/skills/upload',
    opts: formData,
    method: 'post',
    contentType: 'multipart/form-data'
  }) as unknown as Promise<{ success: boolean; message?: string; name?: string; warnings?: string[] }>;
}

/**
 * Toggle whether a skill is active.
 *
 * @param name The skill name to toggle.
 * @param active The desired activation state.
 * @returns `{ success, message? }` from the backend.
 */
export async function setSkillActive(name: string, active: boolean): Promise<{ success: boolean; message?: string }> {
  return fetchApi({
    url: '/skills/toggle',
    opts: { name, active },
    method: 'post'
  }) as unknown as Promise<{ success: boolean; message?: string }>;
}

/**
 * Delete an auto skill from disk.
 *
 * This is an irreversible operation — the client MUST show a confirmation
 * dialog before calling this. Pinned skills are rejected by the
 * backend (`delete_skill`).
 *
 * @param name The auto skill name to delete.
 * @returns `{ success, name?, message? }` from the backend.
 */
export async function deleteSkill(name: string): Promise<DeleteSkillResponse> {
  return fetchApi({
    url: '/skills/delete',
    opts: { name },
    method: 'post'
  }) as unknown as Promise<DeleteSkillResponse>;
}

/**
 * Pin or unpin an auto skill.
 *
 * Pinned skills are excluded from curator merging/removal and rejected by the
 * backend `delete_skill`. When `pinned` is `true` the skill is protected; when
 * `false` it is returned to normal curator lifecycle.
 *
 * @param name The auto skill name to pin/unpin.
 * @param pinned `true` to pin, `false` to unpin.
 * @returns `{ success, name?, pinned?, message? }` from the backend.
 */
export async function pinSkill(name: string, pinned: boolean): Promise<PinSkillResponse> {
  return fetchApi({
    url: '/skills/pin',
    opts: { name, pinned },
    method: 'post'
  }) as unknown as Promise<PinSkillResponse>;
}
