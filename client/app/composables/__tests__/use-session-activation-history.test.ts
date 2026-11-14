/**
 * The history-loading skeleton grace period: a new session's empty history
 * resolves immediately and must not flash the skeleton; a slower switch does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import type { MessageItem } from '@/pages/home/type';
import type { ChatController } from '../messages';
import { useSessionActivation } from '../use-session-activation';

function makeHarness(syncHistory: (sid: string) => Promise<void>) {
  const sessionId = ref('sid-a');
  const deps = {
    mySid: 'sid-a',
    sessionId,
    chatMessages: ref<MessageItem[]>([]),
    isSending: ref(false),
    activeAgentController: ref<ChatController | null>(null),
    draft: ref(''),
    hitl: { abortResume: vi.fn(), clearRequest: vi.fn() },
    chatInput: () => null,
    clearQueueBadge: vi.fn(),
    clearMediaSelection: vi.fn(),
    ensureSessionCharacter: vi.fn(),
    refreshBranchLink: vi.fn(),
    syncHistory,
    restorePendingHitl: vi.fn(),
    initStores: vi.fn(),
    registerFindShortcut: vi.fn(),
    unregisterFindShortcut: vi.fn(),
    openFindInConversation: vi.fn()
  };
  const scope = effectScope();
  const activation = scope.run(() => useSessionActivation(deps))!;
  return { activation, scope, sessionId };
}

describe('useSessionActivation history skeleton', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not show the skeleton for a fast (new-session) load', async () => {
    const syncHistory = vi.fn(async () => {});
    const { activation, scope } = makeHarness(syncHistory);

    await vi.advanceTimersByTimeAsync(0);
    expect(activation.isHistoryLoading.value).toBe(false);
    vi.advanceTimersByTime(200);
    expect(activation.isHistoryLoading.value).toBe(false);
    scope.stop();
  });

  it('shows the skeleton only after 200 ms and clears it when history lands', async () => {
    let finish: () => void = () => {};
    const syncHistory = vi.fn(
      () =>
        new Promise<void>(resolve => {
          finish = resolve;
        })
    );
    const { activation, scope } = makeHarness(syncHistory);

    expect(activation.isHistoryLoading.value).toBe(false);
    vi.advanceTimersByTime(199);
    expect(activation.isHistoryLoading.value).toBe(false);
    vi.advanceTimersByTime(1);
    expect(activation.isHistoryLoading.value).toBe(true);

    finish();
    await vi.advanceTimersByTimeAsync(0);
    expect(activation.isHistoryLoading.value).toBe(false);
    scope.stop();
  });
});
