/**
 * The git bridge's failure contract: every call THROWS on a failure.
 *
 * Both halves matter operationally: the writes go through the raw transport so a
 * refused `checkout` reaches the caller with git's own reason ("Your local
 * changes would be overwritten…"), and the reads reject instead of resolving to
 * the shared client's null payload — the panel assigns that result to its state,
 * and a null `page` threw on every later render (row clicks and branch switches
 * stopped working until a full page reload).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  fetchApiPayload: vi.fn(),
  fetchApiRaw: vi.fn()
}));

vi.mock('../requestApi', () => ({
  fetchApiPayload: mocks.fetchApiPayload,
  fetchApiRaw: mocks.fetchApiRaw
}));

import { checkoutGitRef, fetchCommitFiles, fetchGitGraph, resetGitBranch } from '../bridge/git';

/**
 * A fetch-like response double for the raw transport.
 * @param status
 * @param body
 */
function response(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body
  };
}

describe('git bridge', () => {
  beforeEach(() => {
    mocks.fetchApiPayload.mockReset();
    mocks.fetchApiRaw.mockReset();
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

  it("carries git's reason out of a refused checkout", async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(
      response(409, {
        success: false,
        reason: 'error: Your local changes to the following files would be overwritten by checkout: src/app.ts'
      })
    );

    await expect(checkoutGitRef('sid-1', 'main')).rejects.toThrow('would be overwritten by checkout');

    // The write went over the raw transport as a JSON POST.
    expect(mocks.fetchApiRaw).toHaveBeenCalledWith(
      expect.objectContaining({
        url: '/git/checkout',
        method: 'post',
        contentType: 'application/json'
      })
    );
    expect(JSON.parse(String(mocks.fetchApiRaw.mock.calls[0]![0].body))).toEqual({
      session_id: 'sid-1',
      ref: 'main'
    });
  });

  it('answers the refreshed page for an accepted checkout', async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(
      response(200, { success: true, available: true, branch: 'main', branches: ['main', 'dev'] })
    );

    const page = await checkoutGitRef('sid-1', 'main');

    expect(page.branch).toBe('main');
    expect(page.branches).toEqual(['main', 'dev']);
  });

  it('falls back to a status-named reason when the body has none', async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(response(500, { success: false }));

    await expect(resetGitBranch('sid-1', 'abcdef12', 'soft')).rejects.toThrow('git reset failed (HTTP 500)');
  });
});
