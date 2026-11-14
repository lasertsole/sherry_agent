/**
 * Extraction-settings bridge (预设角色 → 高级).
 *
 * Four user-facing values in two scopes:
 *
 * - **global** — the app-wide ``sherry.jsonc`` ``extraction`` group
 *   ({@link fetchGlobalExtraction} / {@link saveGlobalExtraction}); a change
 *   applies immediately to every session that has not overridden the field.
 * - **session** — this session's overrides ({@link fetchExtractionState} /
 *   {@link saveSessionExtraction}); a write while a turn is in flight is
 *   PARKED by the backend and applies from the next turn (``pending: true``),
 *   exactly like the model / thinking controls.
 *
 * Experience learning and the two iteration ceilings are session-overridable;
 * flow replay compilation is global-only, so a session payload never carries
 * it (the backend rejects the field by name).
 *
 * @module bridge/extraction
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol).

/** The four user-facing values. */
export interface ExtractionValues {
  /** Experience learning: the plan-completion skill review. */
  skill_extraction_enabled: boolean;
  /** Flow replay compilation (PTC + LDW). Global-only; absent in session payloads. */
  flow_extraction_enabled?: boolean;
  /** The main agent's per-turn model-call ceiling. */
  iteration_budget_main: number;
  /** The subagents' per-turn ceiling (a cap: a role's own lower limit still wins). */
  iteration_budget_sub: number;
}

/** The session-overridable subset (flow never appears here). */
export type ExtractionOverrides = Partial<Omit<ExtractionValues, 'flow_extraction_enabled'>>;

/** Iteration-ceiling bounds, shared with the backend sanitizer. */
export interface ExtractionBounds {
  min_iterations: number;
  max_iterations: number;
}

/** Everything one Advanced tab renders. */
export interface ExtractionState {
  /** The app-wide values every session inherits. */
  global: ExtractionValues;
  /** This session's overrides ({} = inherit every field). */
  session: ExtractionOverrides;
  /** Session overrides merged over the global values (what the gates act on). */
  effective: ExtractionValues;
  /** True when the last session write is parked until the next turn. */
  pending: boolean;
  /** The built-in defaults (the reset action's target). */
  defaults: ExtractionValues;
  bounds: ExtractionBounds;
}

/** The global-only view (the global panel does not need a session). */
export interface GlobalExtractionState {
  global: ExtractionValues;
  defaults: ExtractionValues;
  bounds: ExtractionBounds;
}

/**
 * Read the session's global + session + effective extraction settings.
 * @param sessionId Main session id (empty = global values only).
 */
export async function fetchExtractionState(sessionId: string): Promise<ExtractionState> {
  const res = await fetchApiPayload<ExtractionState & { success?: boolean }>({
    url: '/sessions/extraction',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return {
    global: res.global,
    session: res.session ?? {},
    effective: res.effective,
    pending: res.pending === true,
    defaults: res.defaults,
    bounds: res.bounds
  };
}

/**
 * Replace the session's extraction overrides.
 * @param sessionId Main session id.
 * @param overrides The full override set ({} = clear every override).
 */
export async function saveSessionExtraction(
  sessionId: string,
  overrides: ExtractionOverrides
): Promise<{ overrides: ExtractionOverrides; effective: ExtractionValues; pending: boolean }> {
  const res = await fetchApiPayload<{
    success?: boolean;
    session: ExtractionOverrides;
    effective: ExtractionValues;
    pending: boolean;
  }>({
    url: '/sessions/extraction',
    opts: { session_id: sessionId, overrides },
    method: 'put'
  });
  return { overrides: res.session ?? {}, effective: res.effective, pending: res.pending === true };
}

/** Read the app-wide extraction settings. */
export async function fetchGlobalExtraction(): Promise<GlobalExtractionState> {
  const res = await fetchApiPayload<GlobalExtractionState & { success?: boolean }>({
    url: '/extraction/global',
    method: 'get'
  });
  return { global: res.global, defaults: res.defaults, bounds: res.bounds };
}

/**
 * Write the app-wide extraction settings (only the fields present are written).
 * @param values The global values to persist.
 */
export async function saveGlobalExtraction(values: Partial<ExtractionValues>): Promise<ExtractionValues> {
  const res = await fetchApiPayload<{ success?: boolean; global: ExtractionValues }>({
    url: '/extraction/global',
    opts: { values },
    method: 'put'
  });
  return res.global;
}
