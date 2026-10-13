/**
 * Session refresh scheduling: fire ahead of expiry, cancel on logout.
 *
 * The timer is driven by the `expires_in` the backend reported (the token itself
 * is an HttpOnly cookie, unreadable from JS). A failed rotation clears the
 * schedule rather than retrying forever — the guard and the transport's 401
 * replay own recovery.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  REFRESH_LEEWAY_SECONDS,
  clearSessionRefresh,
  hasScheduledRefresh,
  scheduleSessionRefresh
} from '@/composables/use-auth-refresh';
import { API_BASE_URL } from '@/composables/env';

/** The rotation itself is `requestApi`'s, so it is observed at the wire: fetch. */
const wire = vi.hoisted(() => vi.fn());

describe('useAuthRefresh', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    wire.mockReset();
    wire.mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', wire);
  });

  afterEach(() => {
    clearSessionRefresh();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('fires the rotation one leeway before expiry', async () => {
    scheduleSessionRefresh(3600);

    await vi.advanceTimersByTimeAsync((3600 - REFRESH_LEEWAY_SECONDS - 1) * 1000);
    expect(wire).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1000);
    expect(wire).toHaveBeenCalledTimes(1);
    expect(wire.mock.calls[0]![0]).toBe(`${API_BASE_URL}/auth/refresh`);
    expect(wire.mock.calls[0]![1]).toMatchObject({ method: 'POST', credentials: 'include' });
    expect(hasScheduledRefresh()).toBe(false);
  });

  it('schedules at least 30 seconds out for a nearly expired token', async () => {
    scheduleSessionRefresh(60);

    await vi.advanceTimersByTimeAsync(29_000);
    expect(wire).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1000);
    expect(wire).toHaveBeenCalledTimes(1);
  });

  it('replaces a previous schedule instead of stacking timers', async () => {
    scheduleSessionRefresh(3600);
    scheduleSessionRefresh(600);

    await vi.advanceTimersByTimeAsync((600 - REFRESH_LEEWAY_SECONDS) * 1000);

    expect(wire).toHaveBeenCalledTimes(1);
  });

  it('clears the schedule when the rotation fails', async () => {
    wire.mockResolvedValue({ ok: false });
    scheduleSessionRefresh(600);

    await vi.advanceTimersByTimeAsync((600 - REFRESH_LEEWAY_SECONDS) * 1000);

    expect(wire).toHaveBeenCalledTimes(1);
    expect(hasScheduledRefresh()).toBe(false);
  });

  it('cancel drops the pending timer', async () => {
    scheduleSessionRefresh(3600);
    expect(hasScheduledRefresh()).toBe(true);

    clearSessionRefresh();

    expect(hasScheduledRefresh()).toBe(false);
    await vi.advanceTimersByTimeAsync(3600 * 1000);
    expect(wire).not.toHaveBeenCalled();
  });
});
