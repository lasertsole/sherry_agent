/**
 * The git bridge's read contract: every call THROWS on a failure.
 *
 * The reads reject instead of resolving to the shared client's null payload —
 * the panel assigns that result to its state, and a null `page` threw on every
 * later render (row clicks stopped working until a full page reload). The write
 * actions (reset / checkout) were removed with the user-visible rollback surface,
 * so no raw-transport path exists here any more.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchApiPayload: vi.fn() }));

vi.mock('../requestApi', () => ({ fetchApiPayload: mocks.fetchApiPayload }));

import { fetchCommitFiles, fetchGitGraph } from '../bridge/git';

describe('git bridge', () => {
  beforeEach(() => {
    mocks.fetchApiPayload.mockReset();
  });

  it('reads a graph page and keeps the branch list', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce({
      success: true,
      available: true,
      branch: 'dev',
      branches: ['dev', 'main'],
      commits: [],
      has_more: false
    });

    const page = await fetchGitGraph('sid-1', { limit: 20 });

    expect(page.branch).toBe('dev');
    expect(page.branches).toEqual(['dev', 'main']);
  });

  it('rejects instead of resolving a null payload', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce(null);

    await expect(fetchGitGraph('sid-1')).rejects.toThrow('git graph failed');
  });

  it('rejects a null commit read too', async () => {
    mocks.fetchApiPayload.mockResolvedValueOnce(null);

    await expect(fetchCommitFiles('sid-1', 'abcdef12')).rejects.toThrow('git commit failed');
  });
});
