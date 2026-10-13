/**
 * Queue-badge lifecycle tests for `useChatStream`.
 *
 * Regression under test (live-verified bug): a send issued while the session is
 * busy is QUEUED, but `handleSend` used to overwrite `streamingTurn` with the
 * queued turn. The still-running turn's next `chunk` then cleared the queued
 * send's badge (the old `clearQueueBadgeForTurn` call in `handleSocketChunk`),
 * so "已排队 · 第 N 位" never rendered. Fix: `handleSocketChunk` never touches
 * badge state; `handleSend` only claims `streamingTurn` when the session was
 * idle, and `turn_started` moves it to the batch's trailing turn.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { defineComponent, ref } from 'vue';
import type { Ref } from 'vue';
import { mount } from '@vue/test-utils';
import type { Composer } from 'vue-i18n';
import { useChatStream, type ChatStreamDeps } from '../use-chat-stream';
import type { MessageItem } from '../../pages/home/type';
import type { AgentSocket, OnChunkCallback, OnDoneCallback, OnHitlCallback, OnQueuedCallback } from '../bridge';
import type { ChatController } from '../messages';
import { CHAT_ROLE } from '@/types/chat-role';

// The composable subscribes to the shared mitt bus (`on`/`off` are unimported
// from `../mitt`); the mock captures the handlers so a test can replay the
// events a real socket/browser would emit.
const mittMocks = vi.hoisted(() => ({ on: vi.fn(), off: vi.fn(), emit: vi.fn() }));
vi.mock('@/composables/mitt', () => mittMocks);

// ---------------------------------------------------------------------------
// Capture the auto-imported `postAgentStream` calls: `use-chat-stream.ts` is
// transformed by unimport and resolves it to `../messages`, so mocking that
// module intercepts the composable's call. Each captured send exposes the
// callbacks the persistent socket would invoke (onChunk/onQueued/onDone) so a
// test can drive the running turn and the queued send independently.
// ---------------------------------------------------------------------------
interface CapturedSend {
  sessionId: string;
  text: string;
  msgId: string | undefined;
  onChunk: OnChunkCallback;
  onDone: OnDoneCallback | undefined;
  onQueued: OnQueuedCallback | undefined;
}

const state = vi.hoisted(() => ({
  markRunningToolsFailed: vi.fn(),
  sends: [] as CapturedSend[],
  // Busy watchdog probe: `active` answers, failNext makes the probe throw.
  turnState: vi.fn(async () => false)
}));

vi.mock('~/composables/bridge/session', () => ({
  fetchTurnState: state.turnState
}));

vi.mock('../messages', async importOriginal => {
  const actual = await importOriginal<typeof import('../messages')>();
  return {
    ...actual,
    postAgentStream: (
      sessionId: string,
      message: { text?: string },
      onChunk: OnChunkCallback,
      onDone?: OnDoneCallback,
      _onError?: (err: unknown) => void,
      _onHitl?: OnHitlCallback,
      onQueued?: OnQueuedCallback,
      msgId?: string
    ): ChatController => {
      state.sends.push({ sessionId, text: message.text ?? '', msgId, onChunk, onDone, onQueued });
      const controller = new AbortController() as ChatController;
      controller.msgId = msgId;
      return controller;
    }
  };
});

/** The composable instance plus the reactive refs a test asserts on. */
interface Harness extends HarnessDeps {
  stream: ReturnType<typeof useChatStream>;
}

/** The collaborator set a harness is built from, without the composable call. */
interface HarnessDeps {
  deps: ChatStreamDeps;
  socket: AgentSocket;
  drafts: ChatStreamDeps['drafts'];
  chatMessages: Ref<MessageItem[]>;
  isSending: Ref<boolean>;
  streamingTurn: Ref<number | null>;
  appendStreamChunk: ReturnType<typeof vi.fn>;
  markRunningToolsFailed: ReturnType<typeof vi.fn>;
}

/**
 * Build one `useChatStream` dependency set with inert collaborators.
 * @returns The deps object plus the reactive refs a test asserts on.
 */
function makeDeps(): HarnessDeps {
  const chatMessages = ref<MessageItem[]>([]);
  const sessionId = ref('s1');
  const draft = ref('');
  const isSending = ref(false);
  const activeAgentController = ref<ChatController | null>(null);
  const streamingTurn = ref<number | null>(null);
  const appendStreamChunk = vi.fn();

  const socket: AgentSocket = {
    sessionId: 's1',
    setHandlers: vi.fn(),
    send: vi.fn(() => ({ controller: { closed: false, abort: vi.fn() }, promise: Promise.resolve() })),
    stop: vi.fn(() => Promise.resolve()),
    sendHitlResponse: vi.fn(),
    sendCancelQueued: vi.fn(),
    sendEditQueued: vi.fn(),
    sendNow: vi.fn(),
    dispose: vi.fn()
  };

  const deps: ChatStreamDeps = {
    chatMessages,
    sessionId,
    mySid: 's1',
    draft,
    isSending,
    activeAgentController,
    socket,
    streamingTurn,
    t: ((key: string) => key) as unknown as Composer['t'],
    getPendingMedia: () => ({ images: [], audios: [], videos: [] }),
    clearMediaSelection: vi.fn(),
    loadSessionHistory: vi.fn(async () => {}),
    drafts: {
      allocateTempId: () => -1,
      activeTurns: () => [],
      trackDraftTurn: vi.fn(),
      writeDraftTurn: vi.fn(async () => {}),
      commitDraftTurn: vi.fn(async () => {}),
      untrackDraftTurn: vi.fn(),
      isDraftTurnActive: () => false
    },
    chunks: { appendStreamChunk, markRunningToolsFailed: state.markRunningToolsFailed },
    hitl: {
      handleHitlRequest: vi.fn(),
      abortResume: vi.fn(),
      clearRequest: vi.fn(),
      isResumeTurn: () => false,
      onTurnFinished: vi.fn(),
      onTurnError: vi.fn()
    }
  };

  return {
    deps,
    socket,
    drafts: deps.drafts,
    chatMessages,
    isSending,
    streamingTurn,
    appendStreamChunk,
    markRunningToolsFailed: state.markRunningToolsFailed
  };
}

/**
 * Build one `useChatStream` instance with inert collaborators.
 * @returns The composable instance plus the reactive refs a test asserts on.
 */
function makeHarness(): Harness {
  const built = makeDeps();
  return { ...built, stream: useChatStream(built.deps) };
}

/**
 * The `ws:connected` handler the most recently built harness registered.
 * @returns The captured handler (throws when no harness subscribed yet).
 */
const lastWsConnectedHandler = (): (() => void) => {
  const handler = mittMocks.on.mock.calls.filter(c => c[0] === 'ws:connected').at(-1)?.[1] as (() => void) | undefined;
  if (!handler) throw new Error('no ws:connected handler registered');
  return handler;
};

beforeEach(() => {
  state.sends = [];
  state.markRunningToolsFailed.mockClear();
  state.turnState.mockReset();
  state.turnState.mockResolvedValue(false);
  mittMocks.on.mockClear();
  mittMocks.off.mockClear();
  mittMocks.emit.mockClear();
  vi.useRealTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useChatStream queue badges', () => {
  it('idle send sets streamingTurn and routes its chunks to its own turn', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('hello');

    expect(harness.isSending.value).toBe(true);
    expect(harness.streamingTurn.value).toBe(1);
    expect(state.sends).toHaveLength(1);
    expect(state.sends[0]!.msgId).toBeTruthy();

    harness.stream.handleSocketChunk('hi there', 'text', 's1');

    expect(harness.appendStreamChunk).toHaveBeenCalledTimes(1);
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'hi there', 'text', 1, undefined);
  });

  it('keeps a queued send badge across the running turn’s chunks without stealing streamingTurn', async () => {
    const harness = makeHarness();

    // Turn 1 starts streaming (idle send).
    await harness.stream.handleSend('first');
    expect(harness.streamingTurn.value).toBe(1);

    // Session is busy → the second send is queued, never starts a turn now.
    await harness.stream.handleSend('second');
    const queued = state.sends[1]!;
    expect(harness.streamingTurn.value).toBe(1);
    expect(harness.isSending.value).toBe(true);

    // Backend confirms the queue position for the second send's msg_id.
    queued.onQueued?.({ sessionId: 's1', position: 1, queueSize: 1, messageId: queued.msgId });
    expect(harness.stream.queueBadgeList.value).toEqual([
      { msgId: queued.msgId, position: 1, queueSize: 1, turn: 2, text: 'second' }
    ]);

    // The running turn's next chunk must NOT clear the queued send's row
    // and must still be routed to the running turn (1), not the queued one (2).
    harness.stream.handleSocketChunk('still running', 'text', 's1');
    expect(harness.stream.queueBadgeList.value).toHaveLength(1);
    expect(harness.streamingTurn.value).toBe(1);
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'still running', 'text', 1, undefined);
  });

  it('closes still-running tool cards when the turn finishes', () => {
    const harness = makeHarness();
    harness.streamingTurn.value = 4;

    harness.stream.handleSocketDone({ modelName: 'stub', inputTokens: 1, outputTokens: 1 });

    // The graph stopped streaming, so a card without a result never finished:
    // leaving it spinning would keep the toolbar's running-command entry alive.
    expect(harness.markRunningToolsFailed).toHaveBeenCalledTimes(1);
  });

  it('ignores a stray done frame when no turn is being tracked', () => {
    const harness = makeHarness();
    harness.streamingTurn.value = null;

    harness.stream.handleSocketDone();

    expect(harness.markRunningToolsFailed).not.toHaveBeenCalled();
  });

  it('turn_started clears the member badges and moves streamingTurn to the trailing turn', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    const queued = state.sends[1]!;
    queued.onQueued?.({ sessionId: 's1', position: 1, queueSize: 1, messageId: queued.msgId });
    expect(harness.stream.queueBadgeList.value).toHaveLength(1);

    // The queued message's turn actually starts: its row leaves the list and the
    // turn receives the streamed reply.
    harness.stream.handleTurnStarted({ sessionId: 's1', turnId: 'turn-1', messageIds: [queued.msgId!] });

    expect(harness.stream.queueBadgeList.value).toHaveLength(0);
    expect(harness.streamingTurn.value).toBe(2);

    harness.stream.handleSocketChunk('batched reply', 'text', 's1');
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'batched reply', 'text', 2, undefined);
  });

  it('delivers each queued message as its own turn with its own reply', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    await harness.stream.handleSend('third');
    const second = state.sends[1]!;
    const third = state.sends[2]!;
    second.onQueued?.({ sessionId: 's1', position: 1, queueSize: 2, messageId: second.msgId });
    third.onQueued?.({ sessionId: 's1', position: 2, queueSize: 2, messageId: third.msgId });
    expect(harness.stream.queueBadgeList.value.map(b => b.text)).toEqual(['second', 'third']);

    // Per-item drain: the backend starts the queued messages as TWO turns, each
    // carried by its own `turn_started` frame, and each gets its own answer.
    harness.stream.handleTurnStarted({
      sessionId: 's1',
      turnId: 'turn-2',
      messageIds: [second.msgId!]
    });
    expect(harness.stream.queueBadgeList.value.map(b => b.text)).toEqual(['third']);
    harness.stream.handleSocketChunk('answer two', 'text', 's1');

    harness.stream.handleTurnStarted({
      sessionId: 's1',
      turnId: 'turn-3',
      messageIds: [third.msgId!]
    });
    harness.stream.handleSocketChunk('answer three', 'text', 's1');

    // Each user bubble is followed by its own AI reply — never one merged answer.
    const rows = harness.chatMessages.value;
    const userTurns = rows
      .filter(m => m.role === CHAT_ROLE.USER && (m.content === 'second' || m.content === 'third'))
      .map(m => m.turn_num);
    expect(userTurns).toEqual([2, 3]);
    for (const turn of [2, 3] as const) {
      const ais = rows.filter(m => m.role === CHAT_ROLE.AI && m.turn_num === turn);
      expect(ais).toHaveLength(1);
    }
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'answer two', 'text', 2, undefined);
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'answer three', 'text', 3, undefined);
    expect(harness.stream.queueBadgeList.value).toHaveLength(0);
  });

  describe('queue row operations', () => {
    /**
     * Queue one message and return its badge row.
     * @param harness
     * @param text
     */
    const queueOne = async (harness: Harness, text: string) => {
      await harness.stream.handleSend(text);
      const send = state.sends[state.sends.length - 1]!;
      send.onQueued?.({ sessionId: 's1', position: 1, queueSize: 1, messageId: send.msgId });
      return send;
    };

    it('cancel sends the frame and drops the row optimistically', async () => {
      const harness = makeHarness();
      const send = await queueOne(harness, 'to cancel');

      harness.stream.cancelQueuedMessage(send.msgId!);

      expect(harness.socket.sendCancelQueued).toHaveBeenCalledWith(send.msgId);
      expect(harness.stream.queueBadgeList.value).toHaveLength(0);
    });

    it('edit updates the row and the stored user message', async () => {
      const harness = makeHarness();
      const send = await queueOne(harness, 'typo her');

      harness.stream.editQueuedMessage(send.msgId!, 'typo here');

      expect(harness.socket.sendEditQueued).toHaveBeenCalledWith(send.msgId, 'typo here');
      expect(harness.stream.queueBadgeList.value[0]!.text).toBe('typo here');
      const row = harness.chatMessages.value.find(m => m.content === 'typo here');
      expect(row).toBeDefined();
    });

    it('send-now sends the frame and drops the row', async () => {
      const harness = makeHarness();
      const send = await queueOne(harness, 'urgent');

      harness.stream.sendNow(send.msgId!);

      expect(harness.socket.sendNow).toHaveBeenCalledWith(send.msgId);
      expect(harness.stream.queueBadgeList.value).toHaveLength(0);
    });

    it('a refused send-now ack leaves the client without a phantom row', async () => {
      const harness = makeHarness();
      const send = await queueOne(harness, 'urgent');

      harness.stream.sendNow(send.msgId!);
      harness.stream.handleSendNowAck({ sessionId: 's1', msgId: send.msgId!, ok: false });

      // The row was dropped optimistically and the backend kept it queued; the
      // next `queued` frame re-adds it, so the list never shows a stale row.
      expect(harness.stream.queueBadgeList.value).toHaveLength(0);
      await queueOne(harness, 'fresh');
      expect(harness.stream.queueBadgeList.value).toHaveLength(1);
    });

    it('an ok cancel ack is idempotent and a foreign session is ignored', async () => {
      const harness = makeHarness();
      const send = await queueOne(harness, 'x');

      harness.stream.handleQueuedCancelled({ sessionId: 's1', msgId: send.msgId!, ok: true });
      expect(harness.stream.queueBadgeList.value).toHaveLength(0);

      harness.stream.handleQueuedCancelled({ sessionId: 'other', msgId: 'x', ok: true });
      expect(harness.stream.queueBadgeList.value).toHaveLength(0);
    });
  });
});

// ---------------------------------------------------------------------------
// Busy-state recovery: the composer must never stay "streaming" after the turn
// ended. Live bug: a long answer was cut at its output cap, the server sent
// `done`, the page never cleared isSending, and 停止 + "继续输入消息以排队"
// stayed on until a manual reload.
// ---------------------------------------------------------------------------
describe('useChatStream busy-state recovery', () => {
  it('releases the composer synchronously on done even when the draft write rejects', async () => {
    const harness = makeHarness();
    await harness.stream.handleSend('count to 2000');
    expect(harness.isSending.value).toBe(true);
    // A failing Dexie commit must not keep the stop button on (the clear used
    // to live inside its .then, so a rejected write leaked the busy state).
    (harness.drafts.commitDraftTurn as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('dexie unavailable'));

    state.sends[0]!.onDone?.({ modelName: 'stub', inputTokens: 1, outputTokens: 1 });

    expect(harness.isSending.value).toBe(false);
    expect(harness.streamingTurn.value).toBeNull();
  });

  it('settles and reloads the history when a done frame arrives without a tracked turn', async () => {
    const harness = makeHarness();
    // The page believes it is streaming, but its turn pointer is gone.
    harness.isSending.value = true;
    harness.streamingTurn.value = null;

    harness.stream.handleSocketDone({ modelName: 'stub' });

    expect(harness.isSending.value).toBe(false);
    expect(state.markRunningToolsFailed).toHaveBeenCalled();
  });

  it('reloads the history for a done frame of a turn this page never sent', async () => {
    // A SERVER-initiated turn (the plan-continuation auto-turn, a subagent
    // completion, a cron run) has no local send entry and gets no
    // `turn_started` (the auto-turn passes no turn identity), so its chunks
    // cannot be streamed. The finished answer IS persisted — without this
    // refetch the continuation stayed invisible until a manual reload.
    const harness = makeHarness();
    harness.isSending.value = false;
    harness.streamingTurn.value = null;

    harness.stream.handleSocketDone({ modelName: 'stub' });

    expect(harness.isSending.value).toBe(false);
    expect(state.markRunningToolsFailed).not.toHaveBeenCalled();
    await Promise.resolve(); // flush the fire-and-forget refetch
    expect(harness.deps.loadSessionHistory).toHaveBeenCalledWith('s1');
  });

  it('watchdog settles the turn once the server reports it is no longer active', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    expect(harness.isSending.value).toBe(true);

    // Server says: no turn in flight any more (the terminal frame was lost).
    state.turnState.mockResolvedValue(false);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(harness.isSending.value).toBe(true); // one reading is not enough
    await vi.advanceTimersByTimeAsync(5_000);
    expect(harness.isSending.value).toBe(false);
    expect(harness.streamingTurn.value).toBeNull();
    expect(state.turnState).toHaveBeenCalled();
  });

  it('watchdog never settles while the server still reports an active turn', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    state.turnState.mockResolvedValue(true);

    await vi.advanceTimersByTimeAsync(30_000);

    expect(harness.isSending.value).toBe(true);
    expect(harness.streamingTurn.value).toBe(1);
  });

  it('watchdog ignores probe failures and stops after the turn settles', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('x');

    state.turnState.mockRejectedValue(new Error('offline'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(harness.isSending.value).toBe(true);

    // A normal terminal frame settles the turn and silences the watchdog.
    state.sends[0]!.onDone?.({ modelName: 'stub' });
    const callsAfterSettle = state.turnState.mock.calls.length;
    await vi.advanceTimersByTimeAsync(20_000);
    expect(state.turnState.mock.calls.length).toBe(callsAfterSettle);
  });
});

// ---------------------------------------------------------------------------
// Recovery triggers: a 5s tick is the fallback, but two moments are stronger
// evidence that the page is runnable again — coming back into view after being
// starved, and the socket reconnecting (frames may have been lost while it was
// down). Both re-ask immediately and once more 1.2s later, because a single
// idle reading could race a just-started turn.
// ---------------------------------------------------------------------------
describe('useChatStream busy reconcile triggers', () => {
  it('settles a stuck turn the moment the page becomes visible again', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    expect(harness.isSending.value).toBe(true);
    // The turn is over server-side; the page just never got the frame.
    state.turnState.mockResolvedValue(false);

    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(1_200);

    expect(harness.isSending.value).toBe(false);
    expect(harness.streamingTurn.value).toBeNull();
  });

  it('keeps the composer busy while the server still reports an active turn', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    state.turnState.mockResolvedValue(true);

    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(1_200);

    expect(harness.isSending.value).toBe(true);
    expect(harness.streamingTurn.value).toBe(1);
  });

  it('re-probes when the socket (re)connects after a dropped frame', async () => {
    vi.useFakeTimers();
    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    state.turnState.mockResolvedValue(false);

    lastWsConnectedHandler()();
    await vi.advanceTimersByTimeAsync(1_200);

    expect(harness.isSending.value).toBe(false);
    expect(harness.streamingTurn.value).toBeNull();
  });

  it('does not probe on a ws:connected frame while the composer is idle', () => {
    makeHarness();

    lastWsConnectedHandler()();

    expect(state.turnState).not.toHaveBeenCalled();
  });

  it('immediately re-probes twice when a watchdog tick runs late (starved page)', async () => {
    // Timers are faked but Date is real: the test advances the wall clock
    // without advancing the interval schedule — exactly what a frozen page
    // produces (the next tick fires far later than its 5s interval).
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'] });
    let fakeNow = 1_000_000;
    vi.spyOn(Date, 'now').mockImplementation(() => fakeNow);

    const harness = makeHarness();
    await harness.stream.handleSend('long answer');
    state.turnState.mockResolvedValue(false);

    fakeNow += 20_000;
    await vi.advanceTimersByTimeAsync(5_000);
    // The late tick probes immediately — one idle reading is not enough.
    expect(state.turnState).toHaveBeenCalledTimes(1);
    expect(harness.isSending.value).toBe(true);

    // The follow-up probe 1.2s later is the second reading and settles.
    await vi.advanceTimersByTimeAsync(1_200);
    expect(state.turnState).toHaveBeenCalledTimes(2);
    expect(harness.isSending.value).toBe(false);
    expect(harness.streamingTurn.value).toBeNull();
  });

  it('detaches the visibility and bus listeners on unmount', () => {
    const built = makeDeps();
    const removeSpy = vi.spyOn(document, 'removeEventListener');
    const Host = defineComponent({
      setup() {
        useChatStream(built.deps);
        return {};
      },
      template: '<div/>'
    });
    const wrapper = mount(Host);

    wrapper.unmount();

    expect(mittMocks.off.mock.calls.some(c => c[0] === 'ws:connected')).toBe(true);
    expect(removeSpy.mock.calls.some(c => c[0] === 'visibilitychange')).toBe(true);
  });
});
