import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { decideReconnect, ReconnectTimer } from '../bridge/agent-socket-reconnect';

describe('decideReconnect', () => {
  it('retries exponentially while the pre-chunk send has budget left', () => {
    expect(decideReconnect(true, 0)).toEqual({ attempt: 1, delayMs: 1000, retrying: true, budgetExhausted: false });
    expect(decideReconnect(true, 1)).toEqual({ attempt: 2, delayMs: 2000, retrying: true, budgetExhausted: false });
    expect(decideReconnect(true, 2)).toEqual({ attempt: 3, delayMs: 4000, retrying: true, budgetExhausted: false });
  });

  it('falls back to the fixed 5s cadence and resets the attempt once the budget is exhausted', () => {
    expect(decideReconnect(true, 3)).toEqual({
      attempt: 0,
      delayMs: 5000,
      retrying: false,
      budgetExhausted: true
    });
  });

  it('quietly keeps the liveness cadence (no resumable send, attempt reset)', () => {
    expect(decideReconnect(false, 0)).toEqual({ attempt: 0, delayMs: 5000, retrying: false, budgetExhausted: false });
    expect(decideReconnect(false, 7)).toEqual({ attempt: 0, delayMs: 5000, retrying: false, budgetExhausted: false });
  });
});

describe('ReconnectTimer', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('fires the callback once after the delay', () => {
    const onFire = vi.fn();
    const timer = new ReconnectTimer();
    timer.schedule(1000, onFire);
    vi.advanceTimersByTime(999);
    expect(onFire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onFire).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10000);
    expect(onFire).toHaveBeenCalledTimes(1);
  });

  it('clear() cancels a pending fire', () => {
    const onFire = vi.fn();
    const timer = new ReconnectTimer();
    timer.schedule(1000, onFire);
    timer.clear();
    vi.advanceTimersByTime(5000);
    expect(onFire).not.toHaveBeenCalled();
  });

  it('re-scheduling replaces the previous pending fire', () => {
    const first = vi.fn();
    const second = vi.fn();
    const timer = new ReconnectTimer();
    timer.schedule(1000, first);
    timer.schedule(2000, second);
    vi.advanceTimersByTime(1000);
    expect(first).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1000);
    expect(second).toHaveBeenCalledTimes(1);
  });
});
