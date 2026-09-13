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
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import type { Ref } from 'vue';
import type { Composer } from 'vue-i18n';
import { useChatStream, type ChatStreamDeps } from '../use-chat-stream';
import type { MessageItem } from '../../pages/home/type';
import type { AgentSocket, OnChunkCallback, OnDoneCallback, OnHitlCallback, OnQueuedCallback } from '../bridge';
import type { ChatController } from '../messages';
import { CHAT_ROLE } from '@/types/chat-role';

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
  sends: [] as CapturedSend[]
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
interface Harness {
  stream: ReturnType<typeof useChatStream>;
  chatMessages: Ref<MessageItem[]>;
  isSending: Ref<boolean>;
  streamingTurn: Ref<number | null>;
  appendStreamChunk: ReturnType<typeof vi.fn>;
}

/**
 * Build one `useChatStream` instance with inert collaborators.
 * @returns The composable instance plus the reactive refs a test asserts on.
 */
function makeHarness(): Harness {
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
    setTasksTabActive: vi.fn(),
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
    chunks: { appendStreamChunk, markRunningToolsFailed: vi.fn() },
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
    stream: useChatStream(deps),
    chatMessages,
    isSending,
    streamingTurn,
    appendStreamChunk
  };
}

beforeEach(() => {
  state.sends = [];
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
    expect(harness.stream.queueBadge.value).toEqual({ position: 1, queueSize: 1 });

    // The running turn's next chunk must NOT clear the queued send's badge
    // and must still be routed to the running turn (1), not the queued one (2).
    harness.stream.handleSocketChunk('still running', 'text', 's1');
    expect(harness.stream.queueBadge.value).toEqual({ position: 1, queueSize: 1 });
    expect(harness.streamingTurn.value).toBe(1);
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'still running', 'text', 1, undefined);
  });

  it('turn_started clears the member badges and moves streamingTurn to the trailing turn', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    const queued = state.sends[1]!;
    queued.onQueued?.({ sessionId: 's1', position: 1, queueSize: 1, messageId: queued.msgId });
    expect(harness.stream.queueBadge.value).not.toBeNull();

    // The queued batch actually starts: every member badge is cleared and the
    // trailing member's turn receives the (single) streamed reply.
    harness.stream.handleTurnStarted({ sessionId: 's1', turnId: 'turn-1', messageIds: [queued.msgId!] });

    expect(harness.stream.queueBadge.value).toBeNull();
    expect(harness.streamingTurn.value).toBe(2);

    harness.stream.handleSocketChunk('batched reply', 'text', 's1');
    expect(harness.appendStreamChunk).toHaveBeenCalledWith('s1', 'batched reply', 'text', 2, undefined);
  });

  it('unifies a multi-message batch onto one turn with a single trailing AI reply', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    await harness.stream.handleSend('third');
    const second = state.sends[1]!;
    const third = state.sends[2]!;
    second.onQueued?.({ sessionId: 's1', position: 1, queueSize: 2, messageId: second.msgId });
    third.onQueued?.({ sessionId: 's1', position: 2, queueSize: 3, messageId: third.msgId });
    expect(harness.stream.queueBadge.value).not.toBeNull();

    harness.stream.handleTurnStarted({
      sessionId: 's1',
      turnId: 'turn-batch',
      messageIds: [second.msgId!, third.msgId!]
    });

    const rows = harness.chatMessages.value;
    // Both queued user messages are unified onto the earliest member's turn (2),
    // matching the single turn the backend persists the batch as.
    expect(
      rows.filter(m => m.role === CHAT_ROLE.USER && m.turn_num === 2).map(m => m.content)
    ).toEqual(['second', 'third']);
    // Exactly ONE AI placeholder for the batch, at the unified turn, after both users.
    const ais = rows.filter(m => m.role === CHAT_ROLE.AI && m.turn_num === 2);
    expect(ais).toHaveLength(1);
    expect(rows.indexOf(ais[0]!)).toBeGreaterThan(rows.findIndex(m => m.content === 'third'));
    expect(harness.streamingTurn.value).toBe(2);
    expect(harness.stream.queueBadge.value).toBeNull();
  });
});
