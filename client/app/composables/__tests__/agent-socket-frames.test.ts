import { describe, it, expect, vi } from 'vitest';
import { routeAgentFrame, type AgentFrameContext } from '../bridge/agent-socket-frames';
import type { AgentSocketHandlers } from '../bridge/agent-socket-types';
import { on, off } from '../mitt';

type Turn = { turnId: string; msgIds: string[] } | null;

function makeCtx(handlers: AgentSocketHandlers = {}, pendingKeys: string[] = ['p1']) {
  let activeTurn: Turn = null;
  const ctx: AgentFrameContext = {
    sessionId: 'sess',
    handlers,
    markAllReceivedChunk: vi.fn(),
    settleResolve: vi.fn(),
    settleReject: vi.fn(),
    getActiveTurn: () => activeTurn,
    setActiveTurn: turn => {
      activeTurn = turn;
    },
    pendingKeys: () => pendingKeys,
    flushStopResolvers: vi.fn()
  };
  return { ctx, getActiveTurn: () => activeTurn };
}

const fire = (ctx: AgentFrameContext, data: unknown) =>
  routeAgentFrame({ data: JSON.stringify(data) } as MessageEvent, ctx);

describe('routeAgentFrame', () => {
  it('chunk: marks sends as chunked and forwards content/type/session/meta', () => {
    const onChunk = vi.fn();
    const { ctx } = makeCtx({ onChunk });
    fire(ctx, {
      event: 'chunk',
      content: 'hi',
      type: 'tool_result',
      session_id: 's1',
      tool_id: 't',
      tool_name: 'n',
      args: { a: 1 },
      error: true
    });
    expect(ctx.markAllReceivedChunk).toHaveBeenCalledTimes(1);
    expect(onChunk).toHaveBeenCalledWith('hi', 'tool_result', 's1', {
      tool_id: 't',
      tool_name: 'n',
      args: { a: 1 },
      error: true
    });
  });

  it('chunk: defaults type to text, session to the socket session, content to empty', () => {
    const onChunk = vi.fn();
    const { ctx } = makeCtx({ onChunk });
    fire(ctx, { event: 'chunk' });
    expect(onChunk).toHaveBeenCalledWith('', 'text', 'sess', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });
  });

  it('hitl_request: dispatches object content only', () => {
    const onHitl = vi.fn();
    const { ctx } = makeCtx({ onHitl });
    const interrupt = { tool_name: 'x', tool_args: {}, description: '', allowed_decisions: [] };
    fire(ctx, { event: 'hitl_request', content: interrupt });
    expect(onHitl).toHaveBeenCalledWith(interrupt);

    fire(ctx, { event: 'hitl_request', content: 'not-an-object' });
    expect(onHitl).toHaveBeenCalledTimes(1);
  });

  it('queued: forwards position/queue size with defaults', () => {
    const onQueued = vi.fn();
    const { ctx } = makeCtx({ onQueued });
    fire(ctx, { event: 'queued', position: 2, session_id: 's2', queue_size: 3, message_id: 'm' });
    expect(onQueued).toHaveBeenCalledWith({ sessionId: 's2', position: 2, queueSize: 3, messageId: 'm' });

    fire(ctx, { event: 'queued', position: 1 });
    expect(onQueued).toHaveBeenLastCalledWith({
      sessionId: 'sess',
      position: 1,
      queueSize: 0,
      messageId: undefined
    });

    fire(ctx, { event: 'queued', position: 'x' });
    expect(onQueued).toHaveBeenCalledTimes(2);
  });

  it('turn_started: records the active turn and notifies the handler', () => {
    const onTurnStarted = vi.fn();
    const { ctx, getActiveTurn } = makeCtx({ onTurnStarted });
    fire(ctx, { event: 'turn_started', turn_id: 't1', message_ids: ['a', 'b'], session_id: 's1' });
    expect(getActiveTurn()).toEqual({ turnId: 't1', msgIds: ['a', 'b'] });
    expect(onTurnStarted).toHaveBeenCalledWith({ sessionId: 's1', turnId: 't1', messageIds: ['a', 'b'] });
  });

  it('done: resolves the turn sends, clears the turn and forwards model metadata', () => {
    const onDone = vi.fn();
    const { ctx, getActiveTurn } = makeCtx({ onDone });
    fire(ctx, { event: 'done', message_ids: ['a'], model_name: 'm', input_tokens: 1, output_tokens: 2 });
    expect(ctx.settleResolve).toHaveBeenCalledWith(['a']);
    expect(getActiveTurn()).toBeNull();
    expect(onDone).toHaveBeenCalledWith({ modelName: 'm', inputTokens: 1, outputTokens: 2 });
  });

  it('done: falls back to the active turn ids, then to every pending key', () => {
    const { ctx } = makeCtx({});
    fire(ctx, { event: 'turn_started', turn_id: 't', message_ids: ['a'] });
    fire(ctx, { event: 'done' });
    expect(ctx.settleResolve).toHaveBeenLastCalledWith(['a']);

    const { ctx: ctx2 } = makeCtx({}, ['p1', 'p2']);
    fire(ctx2, { event: 'done' });
    expect(ctx2.settleResolve).toHaveBeenCalledWith(['p1', 'p2']);
  });

  it('error: rejects the turn sends with the frame message (or a default)', () => {
    const { ctx } = makeCtx({});
    fire(ctx, { event: 'error', message_ids: ['a'], content: 'boom' });
    expect(ctx.settleReject).toHaveBeenCalledWith(['a'], expect.objectContaining({ message: 'boom' }));

    fire(ctx, { event: 'error', content: 42 });
    expect(ctx.settleReject).toHaveBeenLastCalledWith(
      ['p1'],
      expect.objectContaining({ message: 'WebSocket stream error' })
    );
  });

  it('stopped: resolves the turn sends and flushes stop waiters', () => {
    const { ctx } = makeCtx({}, ['p1']);
    fire(ctx, { event: 'stopped' });
    expect(ctx.settleResolve).toHaveBeenCalledWith(['p1']);
    expect(ctx.flushStopResolvers).toHaveBeenCalledTimes(1);
  });

  it('todo_updated: re-emits on the mitt bus', () => {
    const spy = vi.fn();
    on('ws:todo_updated', spy);
    const { ctx } = makeCtx({});
    fire(ctx, { event: 'todo_updated', content: 'x' });
    expect(spy).toHaveBeenCalledWith({ event: 'todo_updated', content: 'x' });
    off('ws:todo_updated', spy);
  });

  it('ignores non-JSON and non-object frames without throwing', () => {
    const onChunk = vi.fn();
    const { ctx } = makeCtx({ onChunk });
    expect(() => routeAgentFrame({ data: 'not json' } as MessageEvent, ctx)).not.toThrow();
    expect(() => routeAgentFrame({ data: '42' } as MessageEvent, ctx)).not.toThrow();
    expect(onChunk).not.toHaveBeenCalled();
  });
});
