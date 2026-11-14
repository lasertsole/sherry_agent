/**
 * The extraction bridge's request/response contract.
 *
 * Both scopes are pinned: the session call carries the session id + the full
 * override set and reports the parked flag, while the global call writes only
 * the values it is given (the backend merges partial payloads).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchApiPayload: vi.fn() }));

vi.mock('../requestApi', () => ({ fetchApiPayload: mocks.fetchApiPayload }));

import {
  fetchExtractionState,
  fetchGlobalExtraction,
  saveGlobalExtraction,
  saveSessionExtraction
} from '../bridge/extraction';

const GLOBAL = {
  skill_extraction_enabled: true,
  flow_extraction_enabled: true,
  iteration_budget_main: 180,
  iteration_budget_sub: 90
};

describe('extraction bridge', () => {
  beforeEach(() => {
    mocks.fetchApiPayload.mockReset();
  });

  it('reads the session state from the session-scoped endpoint', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce({
      success: true,
      global: GLOBAL,
      session: { skill_extraction_enabled: false },
      effective: { ...GLOBAL, skill_extraction_enabled: false, flow_extraction_enabled: false },
      pending: true,
      defaults: GLOBAL,
      bounds: { min_iterations: 1, max_iterations: 1000 }
    });

    const state = await fetchExtractionState('sid-1');

    expect(mocks.fetchApiPayload).toHaveBeenCalledWith({
      url: '/sessions/extraction',
      opts: { session_id: 'sid-1' },
      method: 'get'
    });
    expect(state.session).toEqual({ skill_extraction_enabled: false });
    expect(state.effective.flow_extraction_enabled).toBe(false);
    expect(state.pending).toBe(true);
    expect(state.bounds.max_iterations).toBe(1000);
  });

  it('writes the complete override set and reports the parked flag', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce({
      success: true,
      session: { iteration_budget_main: 120 },
      effective: { ...GLOBAL, iteration_budget_main: 120 },
      pending: false
    });

    const result = await saveSessionExtraction('sid-1', { iteration_budget_main: 120 });

    expect(mocks.fetchApiPayload).toHaveBeenCalledWith({
      url: '/sessions/extraction',
      opts: { session_id: 'sid-1', overrides: { iteration_budget_main: 120 } },
      method: 'put'
    });
    expect(result.overrides).toEqual({ iteration_budget_main: 120 });
    expect(result.pending).toBe(false);
  });

  it('reads and writes the global scope through its own endpoint', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce({
      success: true,
      global: GLOBAL,
      defaults: GLOBAL,
      bounds: { min_iterations: 1, max_iterations: 1000 }
    });

    const state = await fetchGlobalExtraction();
    expect(mocks.fetchApiPayload).toHaveBeenCalledWith({
      url: '/extraction/global',
      method: 'get'
    });
    expect(state.global).toEqual(GLOBAL);

    mocks.fetchApiPayload.mockResolvedValueOnce({
      success: true,
      global: { ...GLOBAL, iteration_budget_main: 60 }
    });
    const updated = await saveGlobalExtraction({ iteration_budget_main: 60 });
    expect(mocks.fetchApiPayload).toHaveBeenLastCalledWith({
      url: '/extraction/global',
      opts: { values: { iteration_budget_main: 60 } },
      method: 'put'
    });
    expect(updated.iteration_budget_main).toBe(60);
  });
});
