/**
 * Reconnect policy + timer for the per-session agent socket.
 *
 * `decideReconnect` is the pure decision core: pre-chunk sends are resent with
 * exponential backoff while the retry budget lasts; once the budget is exhausted
 * (or when nothing is recoverable) the socket falls back to the fixed liveness
 * cadence. `ReconnectTimer` owns the cancellable timeout.
 *
 * @module bridge/agent-socket-reconnect
 */
import { WS_FALLBACK_RECONNECT_MS, WS_RECONNECT_MAX_ATTEMPTS, wsReconnectDelayMs } from './chat-types';

/** The next reconnect action decided from the current attempt and recoverability. */
export interface ReconnectDecision {
  /** Attempt value to store before emitting (0 resets the budget). */
  attempt: number;
  /** Delay in milliseconds before the reconnect attempt. */
  delayMs: number;
  /** True when a resumable pre-chunk send exists and an exponential retry is scheduled. */
  retrying: boolean;
  /** True when a resumable send exists but the exponential retry budget is used up. */
  budgetExhausted: boolean;
}

/**
 * Decide the next reconnect action.
 *
 * Mirrors the previous branching exactly: a resumable pre-chunk send with budget
 * left is retried exponentially (attempt incremented); a resumable send without
 * budget falls back to the fixed cadence and resets the attempt; no in-flight send
 * quietly keeps the socket alive at the fixed cadence.
 * @param hasResumable Whether at least one pre-chunk send can be re-sent
 * @param attempt Current 0-based exponential attempt counter
 */
export function decideReconnect(hasResumable: boolean, attempt: number): ReconnectDecision {
  if (hasResumable && attempt < WS_RECONNECT_MAX_ATTEMPTS) {
    const nextAttempt = attempt + 1;
    return {
      attempt: nextAttempt,
      delayMs: wsReconnectDelayMs(nextAttempt),
      retrying: true,
      budgetExhausted: false
    };
  }
  if (hasResumable) {
    return { attempt: 0, delayMs: WS_FALLBACK_RECONNECT_MS, retrying: false, budgetExhausted: true };
  }
  return { attempt: 0, delayMs: WS_FALLBACK_RECONNECT_MS, retrying: false, budgetExhausted: false };
}

/** Cancellable single-shot timeout used for reconnect scheduling. */
export class ReconnectTimer {
  private timer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Schedule `onFire` after `delayMs`; any previously scheduled fire is replaced.
   * @param delayMs
   * @param onFire
   */
  schedule(delayMs: number, onFire: () => void): void {
    this.clear();
    this.timer = setTimeout(() => {
      this.timer = null;
      onFire();
    }, delayMs);
  }

  /** Cancel a pending fire (no-op when none is scheduled). */
  clear(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
