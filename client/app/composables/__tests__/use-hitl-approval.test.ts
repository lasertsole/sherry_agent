/**
 * Unit tests for `useHitlApproval` decision forwarding.
 *
 * The approval card's decision must reach the backend as a `hitl_response`
 * frame carrying the exact decision value — including the directory-level
 * `approve_dir` grant added by the external-path HITL flow.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import { useHitlApproval } from '../use-hitl-approval';
import type { HitlApprovalDeps } from '../use-hitl-approval';
import type { MessageItem } from '../../pages/home/type';
import type { AgentSocket } from '../bridge';
import type { ChatController } from '../messages';

const state = vi.hoisted(() => ({
  sent: [] as Array<{ decision: string; message?: string }>,
  drafts: {
    trackDraftTurn: vi.fn(),
    writeDraftTurn: vi.fn(async () => {}),
    commitDraftTurn: vi.fn(async () => {}),
    untrackDraftTurn: vi.fn()
  },
  chunks: {
    appendStreamChunk: vi.fn(),
    markRunningToolsFailed: vi.fn()
  },
  stop: vi.fn()
}));

/** Build one `useHitlApproval` instance with inert collaborators. */
function makeHarness() {
  const sessionId = ref('s1');
  const isSending = ref(false);
  const streamingTurn = ref<number | null>(null);
  const activeAgentController = ref<ChatController | null>(null);
  const chatMessages = ref<MessageItem[]>([{ turn_num: 1 } as MessageItem]);
  const loadSessionHistory = vi.fn(async () => {});

  const socket = {
    sessionId: 's1',
    setHandlers: vi.fn(),
    send: vi.fn(),
    stop: state.stop,
    sendHitlResponse: (response: { decision: string; message?: string }) => {
      state.sent.push(response);
    },
    dispose: vi.fn()
  } as unknown as AgentSocket;

  const deps: HitlApprovalDeps = {
    chatMessages,
    sessionId,
    isSending,
    activeAgentController,
    socket,
    streamingTurn,
    loadSessionHistory,
    drafts: state.drafts,
    chunks: state.chunks
  };

  return {
    hitl: useHitlApproval(deps),
    isSending,
    streamingTurn,
    loadSessionHistory
  };
}

describe('useHitlApproval decision forwarding', () => {
  beforeEach(() => {
    state.sent.length = 0;
    vi.clearAllMocks();
  });

  it.each(['approve', 'approve_dir', 'yolo', 'reject'] as const)(
    'sends the %s decision verbatim on the persistent socket',
    decision => {
      const { hitl, isSending } = makeHarness();

      hitl.handleHitlDecision(decision);

      expect(state.sent).toEqual([{ decision, message: '' }]);
      expect(isSending.value).toBe(true);
      expect(hitl.hitlRequest.value).toBeNull();
    }
  );

  it('forwards an accompanying message for the approve_dir decision', () => {
    const { hitl } = makeHarness();

    hitl.handleHitlDecision('approve_dir', 'allow the reports folder');

    expect(state.sent).toEqual([{ decision: 'approve_dir', message: 'allow the reports folder' }]);
  });

  it('treats approve_dir as a grant, not a rejection', () => {
    const { hitl } = makeHarness();

    hitl.handleHitlDecision('approve_dir');

    expect(state.chunks.markRunningToolsFailed).not.toHaveBeenCalled();
    expect(state.drafts.writeDraftTurn).not.toHaveBeenCalled();
    expect(state.drafts.trackDraftTurn).toHaveBeenCalledWith('s1', 2);
  });

  it('marks running tools failed only on reject', () => {
    const { hitl } = makeHarness();

    hitl.handleHitlDecision('reject');

    expect(state.chunks.markRunningToolsFailed).toHaveBeenCalledTimes(1);
  });
});
