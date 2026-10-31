/**
 * The 直播安全 settings tab's backend calls (keyword rules + the filter log).
 *
 * Lives directly under app/composables (not bridge/) so the Nuxt auto-import
 * scanner registers it: the settings panel calls these without an import.
 *
 * The master switch and the LLM review mode/model are ordinary `sherry.jsonc`
 * keys and go through `writeSherryConfig`; only the operator-owned rules FILE
 * and the process-level event log need their own endpoints.
 */

/** One keyword rule as the backend compiled it. */
export interface SafetyRule {
  pattern: string;
  /** exact | contains | regex */
  type: string;
  /** block | replace | warn */
  action: string;
  /** input | output | both */
  scope: string;
  replacement: string;
}

/** The rules snapshot the tab renders. */
export interface SafetyRulesPayload {
  path: string;
  exists: boolean;
  count: number;
  rules: SafetyRule[];
}

/** One rule that matched during a test scan. */
export interface SafetyTestHit {
  pattern: string;
  matched_text: string;
  action: string;
  scope: string;
}

/** One recorded filter event. */
export interface SafetyLogEvent {
  at: number;
  layer: string;
  action: string;
  rule_count: number;
  message: string;
  snippet: string;
}

/**
 * Read the loaded keyword rules.
 * @returns The snapshot (count + rules); throws on a failed request.
 */
export async function readSafetyRules(): Promise<SafetyRulesPayload> {
  const res = await fetchApi<SafetyRulesPayload>({ url: '/safety-filter/rules', method: 'get' });
  return res || { path: '', exists: false, count: 0, rules: [] };
}

/**
 * Validate and persist the rules file.
 * @param rules The rule list as the editor holds it.
 * @returns `{success, message?}` — a refused document writes nothing.
 */
export async function writeSafetyRules(
  rules: SafetyRule[]
): Promise<{ success: boolean; message?: string; count?: number }> {
  const res = await fetchApi<{ success?: boolean; message?: string; count?: number }>({
    url: '/safety-filter/rules',
    method: 'put',
    // `fetchApi` carries the request body on `opts` (the same field
    // writeSherryConfig uses).
    opts: { rules }
  });
  return { success: res?.success === true, message: res?.message, count: res?.count };
}

/**
 * Scan a piece of text against the loaded rules (the tab's 测试 button).
 * @param text Sample text.
 * @param scope Whether to test the input or the output rules.
 */
export async function testSafetyRules(text: string, scope: 'input' | 'output'): Promise<SafetyTestHit[]> {
  const res = await fetchApi<{ hits?: SafetyTestHit[] }>({
    url: '/safety-filter/test',
    method: 'post',
    opts: { text, scope }
  });
  return Array.isArray(res?.hits) ? res.hits : [];
}

/**
 * Recent filter events, newest first.
 * @param sessionId Session to read (omit for the whole process).
 * @param limit Max rows.
 */
export async function readSafetyLog(sessionId?: string, limit = 50): Promise<SafetyLogEvent[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (sessionId) query.set('session_id', sessionId);
  const res = await fetchApi<{ events?: SafetyLogEvent[] }>({
    url: `/safety-filter/log?${query.toString()}`,
    method: 'get'
  });
  return Array.isArray(res?.events) ? res.events : [];
}
