/**
 * Session run report bridge — post-task analysis (token / context / code-intel).
 *
 * Kept in its own module (not appended to bridge/session.ts) so the session
 * bridge's import graph — and the `vi.mock('../bridge')` seam the queued/HITL
 * passthrough tests rely on — stays byte-for-byte unchanged.
 */
import { fetchApiPayload } from '../requestApi';

export interface SessionReport {
  session_id: string;
  generated_at: number;
  model: string;
  tokens: {
    main: { input: number; output: number; total: number };
    children: Array<{ role: string; input: number; output: number; total: number }>;
    grand_total: number;
    sessions: number;
    estimated_cost_usd?: number;
  };
  context_hit_rate: {
    total_tool_calls: number;
    effective: number;
    wasted: number;
    hit_rate: number;
    wasted_by_tool: Record<string, number>;
  };
  code_intel: {
    precise_calls: number;
    greedy_calls: number;
    total_code_intel_calls: number;
    lsp_vs_grep_ratio: number | null;
    degraded_calls: Array<{ tool: string; reason: string }>;
    available: boolean;
  };
  error?: string;
}

/**
 * Read the session run report. `refresh` forces a live regeneration.
 * @param sessionId - session whose report to fetch
 * @param refresh - force a live regeneration instead of the cached report
 * @returns the session run report (token / context-hit-rate / code-intel)
 */
export async function fetchSessionReport(sessionId: string, refresh = false): Promise<SessionReport> {
  return await fetchApiPayload<SessionReport>({
    url: `/sessions/${encodeURIComponent(sessionId)}/report${refresh ? '?refresh=1' : ''}`,
    method: 'get'
  });
}
