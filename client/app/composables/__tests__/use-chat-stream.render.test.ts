/**
 * Live-render contract for consecutively queued sends (live-verified bug).
 *
 * Reproduction of the browser defect: with one turn running, send TWO more
 * messages. The drain runs them one per turn, and after everything finishes the
 * third bubble rendered the SECOND turn's reply and token meta until a page
 * reload fixed it (the persisted history was always correct).
 *
 * Why the existing suite missed it: `use-chat-stream.test.ts` injects a MOCK
 * `chunks.appendStreamChunk`, so the real row-resolution pipeline
 * (`use-stream-chunks.ts` — the tail heuristic for streamed text, the first-AI
 * match for `done` meta) never ran. This file wires the REAL slice, plus the
 * REAL render view-model (`useChatTurnGroups`), and asserts what the user sees:
 * every user bubble is followed by its OWN reply carrying its OWN token meta.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import type { Composer } from 'vue-i18n';
import { useChatStream, type ChatStreamDeps } from '../use-chat-stream';
import { useStreamChunks } from '../use-stream-chunks';
import { useChatTurnGroups } from '../use-chat-turn-groups';
import type { MessageItem } from '../../pages/home/type';
import type { AgentSocket, OnChunkCallback, OnDoneCallback, OnHitlCallback, OnQueuedCallback } from '../bridge';
import type { ChatController } from '../messages';
import { CHAT_ROLE } from '@/types/chat-role';

interface CapturedSend {
  sessionId: string;
  text: string;
  msgId: string | undefined;
  onChunk: OnChunkCallback;
  onDone: OnDoneCallback | undefined;
  onQueued: OnQueuedCallback | undefined;
}

const state = vi.hoisted(() => ({ sends: [] as CapturedSend[] }));

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

const sendOf = (text: string): CapturedSend => {
  const found = state.sends.find(s => s.text === text);
  if (!found) throw new Error(`no captured send for ${text}`);
  return found;
};

/** Harness with the REAL chunk pipeline and the REAL render view-model. */
function makeHarness() {
  const chatMessages = ref<MessageItem[]>([]);
  const sessionId = ref('s1');
  const draft = ref('');
  const isSending = ref(false);
  const activeAgentController = ref<ChatController | null>(null);
  const streamingTurn = ref<number | null>(null);

  let tempId = -1;
  const allocateTempId = () => tempId--;
  /** Turns registered as active drafts (the real slice keys its persistence on this). */
  const trackedTurns = new Set<number>();
  const drafts = {
    allocateTempId,
    activeTurns: () => [...trackedTurns],
    trackDraftTurn: (_sid: string, turn: number) => {
      trackedTurns.add(turn);
    },
    writeDraftTurn: vi.fn(async () => {}),
    scheduleDraftWrite: vi.fn(),
    commitDraftTurn: vi.fn(async () => {}),
    untrackDraftTurn: (_sid: string, turn: number) => {
      trackedTurns.delete(turn);
    },
    isDraftTurnActive: (turn: number | null) => turn !== null && trackedTurns.has(turn)
  };

  const chunks = useStreamChunks(chatMessages, allocateTempId, drafts as never);

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
    drafts,
    chunks,
    hitl: {
      handleHitlRequest: vi.fn(),
      abortResume: vi.fn(),
      clearRequest: vi.fn(),
      isResumeTurn: () => false,
      onTurnFinished: vi.fn(),
      onTurnError: vi.fn()
    }
  };

  const stream = useChatStream(deps);
  const view = useChatTurnGroups(() => chatMessages.value);
  return { stream, chatMessages, view, streamingTurn };
}

/**
 * The rendered rows that follow the user bubble whose text is `text`.
 * @param rows
 * @param text
 */
function replyRowsAfter(rows: MessageItem[], text: string): MessageItem[] {
  const start = rows.findIndex(m => m.role === CHAT_ROLE.USER && m.content === text);
  if (start < 0) return [];
  const out: MessageItem[] = [];
  for (let i = start + 1; i < rows.length; i++) {
    const row = rows[i]!;
    if (row.role === CHAT_ROLE.USER) break;
    out.push(row);
  }
  return out;
}

const replyText = (rows: MessageItem[]): string => rows.map(r => r.content).join('');
const replyMeta = (rows: MessageItem[]): [number | undefined, number | undefined] => [
  rows.find(r => r.inputTokens !== undefined)?.inputTokens,
  rows.find(r => r.outputTokens !== undefined)?.outputTokens
];

describe('live render of consecutively queued sends', () => {
  beforeEach(() => {
    state.sends = [];
  });

  it('gives every queued message its own bubble, reply and token meta', async () => {
    const harness = makeHarness();

    // 1. idle send starts turn 1; two more are queued behind it
    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    await harness.stream.handleSend('third');
    const second = sendOf('second');
    const third = sendOf('third');
    second.onQueued?.({ sessionId: 's1', position: 1, queueSize: 2, messageId: second.msgId });
    third.onQueued?.({ sessionId: 's1', position: 2, queueSize: 2, messageId: third.msgId });

    // 2. turn 1 streams and completes
    harness.stream.handleSocketChunk('111', 'text', 's1');
    harness.stream.handleSocketDone({ modelName: 'm', inputTokens: 100, outputTokens: 10 });

    // 3. drain turn 2 (per-item: its own turn_started / chunks / done)
    harness.stream.handleTurnStarted({ sessionId: 's1', turnId: 't2', messageIds: [second.msgId!] });
    harness.stream.handleSocketChunk('AAA', 'text', 's1');
    harness.stream.handleSocketDone({ modelName: 'm', inputTokens: 200, outputTokens: 2 });

    // 4. drain turn 3
    harness.stream.handleTurnStarted({ sessionId: 's1', turnId: 't3', messageIds: [third.msgId!] });
    harness.stream.handleSocketChunk('BBB', 'text', 's1');
    harness.stream.handleSocketDone({ modelName: 'm', inputTokens: 300, outputTokens: 2 });

    const rendered = harness.view.filteredMessages.value;
    const users = rendered.filter(m => m.role === CHAT_ROLE.USER).map(m => m.content);
    expect(users).toEqual(['first', 'second', 'third'], 'one bubble per message, in order');

    expect(replyText(replyRowsAfter(rendered, 'first'))).toBe('111');
    expect(replyText(replyRowsAfter(rendered, 'second'))).toBe('AAA');
    expect(replyText(replyRowsAfter(rendered, 'third'))).toBe('BBB');

    expect(replyMeta(replyRowsAfter(rendered, 'first'))).toEqual([100, 10]);
    expect(replyMeta(replyRowsAfter(rendered, 'second'))).toEqual([200, 2]);
    expect(replyMeta(replyRowsAfter(rendered, 'third'))).toEqual([300, 2]);
  });

  it('keeps the live rows identical to what the reloaded history renders', async () => {
    const harness = makeHarness();

    await harness.stream.handleSend('first');
    await harness.stream.handleSend('second');
    const second = sendOf('second');
    second.onQueued?.({ sessionId: 's1', position: 1, queueSize: 1, messageId: second.msgId });

    harness.stream.handleSocketChunk('111', 'text', 's1');
    harness.stream.handleSocketDone({ modelName: 'm', inputTokens: 100, outputTokens: 10 });
    harness.stream.handleTurnStarted({ sessionId: 's1', turnId: 't2', messageIds: [second.msgId!] });
    harness.stream.handleSocketChunk('AAA', 'text', 's1');
    harness.stream.handleSocketDone({ modelName: 'm', inputTokens: 200, outputTokens: 2 });

    // What the server persisted for the same session (two turns, one reply each).
    const persisted: MessageItem[] = [
      { session_id: 's1', role: CHAT_ROLE.USER, content: 'first', id: 101, turn_num: 1, timestamp: '' },
      {
        session_id: 's1',
        role: CHAT_ROLE.AI,
        content: '111',
        id: 102,
        turn_num: 1,
        timestamp: '',
        inputTokens: 100,
        outputTokens: 10
      },
      { session_id: 's1', role: CHAT_ROLE.USER, content: 'second', id: 103, turn_num: 2, timestamp: '' },
      {
        session_id: 's1',
        role: CHAT_ROLE.AI,
        content: 'AAA',
        id: 104,
        turn_num: 2,
        timestamp: '',
        inputTokens: 200,
        outputTokens: 2
      }
    ];
    const persistedView = useChatTurnGroups(() => persisted);

    const live = harness.view.filteredMessages.value.map(m => [m.role, m.content, m.inputTokens, m.outputTokens]);
    const reloaded = persistedView.filteredMessages.value.map(m => [m.role, m.content, m.inputTokens, m.outputTokens]);
    expect(live).toEqual(reloaded);
  });
});
