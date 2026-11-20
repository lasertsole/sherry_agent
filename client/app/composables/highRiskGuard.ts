/**
 * High-risk guard panel backend calls (keyword registry + executed-operation
 * ledger). Lives directly under app/composables so Nuxt auto-imports it.
 */

/** One keyword row the backend returns (built-ins included). */
export interface HighRiskKeyword {
  /** Row id; null for a built-in with no override (delete/policy still work by name). */
  id: number | null;
  keyword: string;
  /** block = scan/ask; allow = never scan. */
  policy: string;
  /** builtin | user | agent */
  source: string;
  /** Whether an approved operation already ran (a repeat is refused). */
  executed: boolean;
  result_summary: string;
  /** session id, 'global', or 'builtin'. */
  scope: string;
}

/** One recorded high-risk execution. */
export interface HighRiskOp {
  id: number;
  tool_name: string;
  args_preview: string;
  result_summary: string;
  executed_at: number;
}

/** The panel's one load. */
export interface HighRiskGuardSnapshot {
  keywords: HighRiskKeyword[];
  ops: HighRiskOp[];
}

/**
 * Read the session's keywords and ledger.
 * @param sessionId Session to read.
 */
export async function readHighRiskGuard(sessionId: string): Promise<HighRiskGuardSnapshot> {
  const res = await fetchApi<HighRiskGuardSnapshot>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-keywords`,
    method: 'get'
  });
  return {
    keywords: Array.isArray(res?.keywords) ? res.keywords : [],
    ops: Array.isArray(res?.ops) ? res.ops : []
  };
}

/**
 * Register a keyword.
 * @param sessionId Owning session.
 * @param keyword Keyword text (>= 3 characters; the backend validates).
 * @param scope session (default) or global.
 */
export async function addHighRiskKeyword(
  sessionId: string,
  keyword: string,
  scope: 'session' | 'global' = 'session'
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-keywords`,
    method: 'post',
    opts: { keyword, scope }
  });
  return { success: res?.success === true, message: res?.message };
}

/**
 * Switch one keyword between block and allow.
 * @param sessionId Owning session.
 * @param keyword Keyword text.
 * @param policy New policy.
 */
export async function setHighRiskKeywordPolicy(
  sessionId: string,
  keyword: string,
  policy: 'block' | 'allow'
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-keywords`,
    method: 'put',
    opts: { keyword, policy }
  });
  return { success: res?.success === true, message: res?.message };
}

/**
 * Clear a keyword's executed marker (one more run allowed).
 * @param sessionId Owning session.
 * @param keyword Keyword text.
 */
export async function resetHighRiskKeyword(
  sessionId: string,
  keyword: string
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-keywords/reset`,
    method: 'post',
    opts: { keyword }
  });
  return { success: res?.success === true, message: res?.message };
}

/**
 * Delete one keyword row.
 * @param sessionId Owning session.
 * @param id Row id.
 */
export async function removeHighRiskKeyword(
  sessionId: string,
  id: number
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-keywords/${id}`,
    method: 'delete'
  });
  return { success: res?.success === true, message: res?.message };
}

/**
 * Delete one ledger row (allows an exact repeat).
 * @param sessionId Owning session.
 * @param id Operation id.
 */
export async function removeHighRiskOp(
  sessionId: string,
  id: number
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: `/sessions/${encodeURIComponent(sessionId)}/high-risk-ops/${id}`,
    method: 'delete'
  });
  return { success: res?.success === true, message: res?.message };
}
