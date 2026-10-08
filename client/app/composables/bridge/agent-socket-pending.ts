/**
 * In-flight send registry for the per-session agent socket.
 *
 * Owns the `msg_id -> PendingSend` map and the resolve/reject plumbing so
 * `agent-socket.ts` only orchestrates frames and connection lifecycle. Ordering,
 * settle-once semantics and the "abandon keeps the promise pending" contract are
 * identical to the previous in-class implementation.
 *
 * @module bridge/agent-socket-pending
 */
import type { ChatRequest } from './chat-types';

/** A single in-flight send awaiting its turn's `done` / `error` / `stopped`. */
export interface PendingSend {
  msgId: string;
  request: ChatRequest;
  /** Serialized payload, filled once any base64 media finished uploading. */
  payload: string | null;
  /** At least one chunk of this turn was received (disconnect after this must never resend). */
  receivedChunk: boolean;
  settled: boolean;
  resolve: () => void;
  reject: (err: unknown) => void;
}

/** Per-session registry of in-flight sends keyed by protocol `msg_id`. */
export class PendingSendRegistry {
  private readonly byId = new Map<string, PendingSend>();

  /**
   * Register a new send and return its pending record plus the promise settled by the turn's end.
   * @param msgId Protocol message id
   * @param request The outgoing chat request
   */
  create(msgId: string, request: ChatRequest): { pending: PendingSend; promise: Promise<void> } {
    let resolveFn: () => void = () => {};
    let rejectFn: (err: unknown) => void = () => {};
    const promise = new Promise<void>((resolve, reject) => {
      resolveFn = resolve;
      rejectFn = reject;
    });
    const pending: PendingSend = {
      msgId,
      request,
      payload: null,
      receivedChunk: false,
      settled: false,
      resolve: () => {},
      reject: () => {}
    };
    pending.resolve = () => {
      if (pending.settled) return;
      pending.settled = true;
      this.byId.delete(msgId);
      resolveFn();
    };
    pending.reject = (err: unknown) => {
      if (pending.settled) return;
      pending.settled = true;
      this.byId.delete(msgId);
      rejectFn(err instanceof Error ? err : new Error(String(err)));
    };
    this.byId.set(msgId, pending);
    return { pending, promise };
  }

  get(id: string): PendingSend | undefined {
    return this.byId.get(id);
  }

  values(): IterableIterator<PendingSend> {
    return this.byId.values();
  }

  /** Snapshot of every registered send (safe to mutate while iterating). */
  all(): PendingSend[] {
    return [...this.byId.values()];
  }

  /** Every registered `msg_id` in insertion order. */
  keys(): string[] {
    return [...this.byId.keys()];
  }

  /**
   * Resolve `messageIds`, falling back to all registered sends when empty.
   * @param messageIds
   */
  settleResolve(messageIds: string[]): void {
    const ids = messageIds.length > 0 ? messageIds : this.keys();
    for (const id of ids) this.byId.get(id)?.resolve();
  }

  /**
   * Reject `messageIds`, falling back to all registered sends when empty.
   * @param messageIds
   * @param err
   */
  settleReject(messageIds: string[], err: unknown): void {
    const ids = messageIds.length > 0 ? messageIds : this.keys();
    for (const id of ids) this.byId.get(id)?.reject(err);
  }

  /** Resolve every registered send (teardown must not surface an error). */
  resolveAll(): void {
    for (const pending of this.all()) pending.resolve();
  }

  /**
   * Silently drop every in-flight send: mark each settled and remove it from the
   * registry WITHOUT settling its promise. Used by a user-initiated stop/abort,
   * where the long-standing contract keeps the returned promise pending (so
   * `postAgentStream` never fires `onError`).
   */
  abandonAll(): void {
    for (const pending of this.all()) {
      if (pending.settled) continue;
      pending.settled = true;
      this.byId.delete(pending.msgId);
    }
  }
}
