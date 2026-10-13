/**
 * MAX_TOKEN toast guard tests for `useChatStream.handleSend`.
 *
 * `handleSend` must surface a warn toast only when `/model-config` reports an
 * invalid (< 128K) config; a valid config stays silent and NEVER blocks the send.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import type { Composer } from 'vue-i18n';
import { useChatStream, type ChatStreamDeps } from '../use-chat-stream';
import type { MessageItem } from '../../pages/home/type';
import type { AgentSocket, OnChunkCallback, OnDoneCallback, OnHitlCallback, OnQueuedCallback } from '../bridge';
import type { ChatController } from '../messages';
import type { ModelConfig } from '../model-config';

// `use-chat-stream.ts` resolves both symbols through unimport-injected import
// bindings (vitest.config.ts), so the mocks intercept the sibling modules.
const configState = vi.hoisted(() => ({ value: { valid: true } as Partial<ModelConfig> }));
const getModelConfigCachedMock = vi.hoisted(() => vi.fn(async () => configState.value));

vi.mock('../model-config', () => ({ getModelConfigCached: getModelConfigCachedMock }));
vi.mock('../toast', () => ({ toastWarn: vi.fn() }));

// Capture the auto-imported `postAgentStream` calls so no WebSocket is opened.
const sendState = vi.hoisted(() => ({ count: 0 }));

vi.mock('../messages', async importOriginal => {
  const actual = await importOriginal<typeof import('../messages')>();
  return {
    ...actual,
    postAgentStream: (
      _sessionId: string,
      _message: { text?: string },
      _onChunk: OnChunkCallback,
      _onDone?: OnDoneCallback,
      _onError?: (err: unknown) => void,
      _onHitl?: OnHitlCallback,
      _onQueued?: OnQueuedCallback,
      msgId?: string
    ): ChatController => {
      sendState.count += 1;
      const controller = new AbortController() as ChatController;
      controller.msgId = msgId;
      return controller;
    }
  };
});

import { toastWarn } from '../toast';

const toastWarnMock = vi.mocked(toastWarn);

/** Build one `useChatStream` instance with inert collaborators. */
function makeHarness() {
  const chatMessages = ref<MessageItem[]>([]);
  const sessionId = ref('s1');
  const draft = ref('');
  const isSending = ref(false);
  const activeAgentController = ref<ChatController | null>(null);
  const streamingTurn = ref<number | null>(null);

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
    chunks: { appendStreamChunk: vi.fn(), markRunningToolsFailed: vi.fn() },
    hitl: {
      handleHitlRequest: vi.fn(),
      abortResume: vi.fn(),
      clearRequest: vi.fn(),
      isResumeTurn: () => false,
      onTurnFinished: vi.fn(),
      onTurnError: vi.fn()
    }
  };

  return { stream: useChatStream(deps), chatMessages, isSending };
}

beforeEach(() => {
  configState.value = { valid: true };
  getModelConfigCachedMock.mockClear();
  toastWarnMock.mockClear();
  sendState.count = 0;
});

describe('useChatStream 128K MAX_TOKEN toast guard', () => {
  it('warns (without blocking the send) when the config is invalid', async () => {
    configState.value = { valid: false };
    const harness = makeHarness();

    await harness.stream.handleSend('hello');

    expect(getModelConfigCachedMock).toHaveBeenCalledTimes(1);
    expect(toastWarnMock).toHaveBeenCalledTimes(1);
    expect(toastWarnMock).toHaveBeenCalledWith('chat.tokenGuard.title', 'chat.tokenGuard.detail', 6000);
    // The send still went out through the normal path.
    expect(sendState.count).toBe(1);
    expect(harness.isSending.value).toBe(true);
    expect(harness.chatMessages.value).toHaveLength(2);
  });

  it('stays silent on a valid config', async () => {
    configState.value = { valid: true };
    const harness = makeHarness();

    await harness.stream.handleSend('hello');

    expect(toastWarnMock).not.toHaveBeenCalled();
    expect(sendState.count).toBe(1);
    expect(harness.chatMessages.value).toHaveLength(2);
  });

  it('stays silent and still sends when the config fetch fails', async () => {
    getModelConfigCachedMock.mockRejectedValueOnce(new Error('backend down'));
    const harness = makeHarness();

    await harness.stream.handleSend('hello');

    expect(toastWarnMock).not.toHaveBeenCalled();
    expect(sendState.count).toBe(1);
    expect(harness.isSending.value).toBe(true);
  });
});
