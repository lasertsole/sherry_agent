import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// bridge.ts *explicitly imports* fetchApi from './requestApi' (not a Nuxt
// auto-import), so we mock that module directly.
const mocks = vi.hoisted(() => ({
  fetchApi: vi.fn(),
}));

vi.mock('../requestApi', () => ({
  fetchApi: mocks.fetchApi,
}));

import * as bridge from '../bridge';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.fetchApi.mockReset();
});

/** Minimal WebSocket double used by the browser-mode tests. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  onopen: ((ev: any) => void) | null = null;
  onmessage: ((ev: any) => void) | null = null;
  onerror: ((ev: any) => void) | null = null;
  onclose: ((ev: any) => void) | null = null;
  sent: string[] = [];
  url: string;
  closed = false;
  readyState = FakeWebSocket.CONNECTING;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }
  frame(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.onmessage?.({ data });
  }
  error() {
    this.onerror?.({});
  }
  closeFromServer() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({});
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

// ── HITL type exports ─────────────────────────────────────────────

describe('HITL type exports', () => {
  it('exports HitlInterruptData type', () => {
    // Type-level check — if it compiles, the export exists
    const data: bridge.HitlInterruptData = {
      tool_name: 'terminal',
      tool_args: { command: 'rm -rf /' },
      description: 'Dangerous command',
      allowed_decisions: ['approve', 'reject'],
    };
    expect(data.tool_name).toBe('terminal');
  });

  it('exports HitlResponse type', () => {
    const resp: bridge.HitlResponse = {
      decision: 'approve',
      message: 'Looks fine',
    };
    expect(resp.decision).toBe('approve');
  });

  it('exports OnHitlCallback type', () => {
    const cb: bridge.OnHitlCallback = (data) => {
      expect(data.tool_name).toBeDefined();
    };
    expect(typeof cb).toBe('function');
  });

  it('includes hitl_request in AgentWsEventType', () => {
    const event: bridge.AgentWsEventType = 'hitl_request';
    expect(event).toBe('hitl_request');
  });
});

// ── HITL stream flow (browser WebSocket) ──────────────────────────

describe('streamChatMessage with HITL (browser WebSocket)', () => {
  it('invokes onHitl callback when hitl_request frame arrives', async () => {
    const onChunk = vi.fn();
    const onHitl = vi.fn();
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'do something dangerous' },
      onChunk,
      onHitl,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    // Simulate some text chunks first
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Let me', type: 'text' });
    ws.frame({ event: 'chunk', session_id: 's1', content: ' help', type: 'text' });
    expect(onChunk).toHaveBeenCalledTimes(2);

    // Server sends a HITL interrupt
    const hitlData = {
      tool_name: 'terminal',
      tool_args: { command: 'rm -rf /tmp' },
      description: 'Dangerous command: rm -rf /tmp',
      allowed_decisions: ['approve', 'reject'],
    };
    ws.frame({ event: 'hitl_request', session_id: 's1', content: hitlData });

    expect(onHitl).toHaveBeenCalledTimes(1);
    expect(onHitl).toHaveBeenCalledWith(hitlData);

    // Socket should still be open (waiting for hitl_response)
    expect(ws.closed).toBe(false);

    // Clean up
    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('does not invoke onHitl when callback is not provided', async () => {
    const onChunk = vi.fn();
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    // Server sends a HITL interrupt — should be silently ignored
    ws.frame({
      event: 'hitl_request',
      session_id: 's1',
      content: { tool_name: 'x', tool_args: {}, description: '', allowed_decisions: [] },
    });

    // No error, stream continues
    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
    expect(ws.closed).toBe(true);
  });
});

// ── sendHitlResponse ──────────────────────────────────────────────

describe('StreamController.sendHitlResponse', () => {
  it('sends a hitl_response frame on the open WebSocket', async () => {
    const onChunk = vi.fn();
    const onHitl = vi.fn();
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
      onHitl,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    // Trigger HITL
    ws.frame({
      event: 'hitl_request',
      session_id: 's1',
      content: {
        tool_name: 'terminal',
        tool_args: { command: 'rm -rf /tmp' },
        description: 'Dangerous',
        allowed_decisions: ['approve', 'reject'],
      },
    });

    // Client sends back an approval
    controller.sendHitlResponse!({ decision: 'approve', message: 'OK' });

    expect(ws.sent).toContainEqual(
      JSON.stringify({
        type: 'hitl_response',
        session_id: 's1',
        decision: 'approve',
        message: 'OK',
        edited_args: undefined,
      }),
    );

    // Clean up
    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('sends a reject decision', async () => {
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's2', text: 'hi' },
      vi.fn(),
      vi.fn(),
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    controller.sendHitlResponse!({ decision: 'reject', message: 'No way' });

    const sent = JSON.parse(ws.sent[1]); // [0] is the initial multi_modal_message
    expect(sent.type).toBe('hitl_response');
    expect(sent.decision).toBe('reject');
    expect(sent.message).toBe('No way');

    ws.frame({ event: 'done', session_id: 's2', content: '' });
    await promise;
  });

  it('sends an edit decision with edited_args', async () => {
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's3', text: 'hi' },
      vi.fn(),
      vi.fn(),
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    controller.sendHitlResponse!({
      decision: 'edit',
      message: 'Modified',
      edited_args: { command: 'ls -la' },
    });

    const sent = JSON.parse(ws.sent[1]);
    expect(sent.decision).toBe('edit');
    expect(sent.edited_args).toEqual({ command: 'ls -la' });

    ws.frame({ event: 'done', session_id: 's3', content: '' });
    await promise;
  });

  it('is a no-op when socket is closed', async () => {
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's4', text: 'hi' },
      vi.fn(),
      vi.fn(),
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.frame({ event: 'done', session_id: 's4', content: '' });
    await promise;

    // Socket is now closed — sendHitlResponse should be a no-op
    controller.sendHitlResponse!({ decision: 'approve' });
    // No new frames sent beyond the initial message
    expect(ws.sent.length).toBe(1);
  });

  it('is a no-op when controller is aborted', async () => {
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's5', text: 'hi' },
      vi.fn(),
      vi.fn(),
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();
    controller.abort();

    controller.sendHitlResponse!({ decision: 'approve' });
    // Only the stop frame should have been sent
    expect(ws.sent.length).toBe(2); // [0] = multi_modal_message, [1] = stop
  });
});

// ── HITL + normal streaming interleave ────────────────────────────

describe('HITL interleave with normal streaming', () => {
  it('handles chunk → hitl_request → chunk → done sequence', async () => {
    const onChunk = vi.fn();
    const onHitl = vi.fn();
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'run command' },
      onChunk,
      onHitl,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    // Initial text
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Running', type: 'text' });

    // HITL interrupt
    ws.frame({
      event: 'hitl_request',
      session_id: 's1',
      content: {
        tool_name: 'terminal',
        tool_args: { command: 'rm -rf /tmp' },
        description: 'Approve?',
        allowed_decisions: ['approve', 'reject'],
      },
    });

    // Client approves
    controller.sendHitlResponse!({ decision: 'approve' });

    // Server resumes streaming
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Done!', type: 'text' });
    ws.frame({ event: 'done', session_id: 's1', content: '' });

    await promise;

    expect(onChunk).toHaveBeenCalledTimes(2);
    expect(onChunk).toHaveBeenNthCalledWith(1, 'Running', 'text');
    expect(onChunk).toHaveBeenNthCalledWith(2, 'Done!', 'text');
    expect(onHitl).toHaveBeenCalledTimes(1);
    expect(ws.closed).toBe(true);
  });

  it('handles multiple consecutive hitl_requests', async () => {
    const onHitl = vi.fn();
    const { controller, promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'run multiple commands' },
      vi.fn(),
      onHitl,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    // First HITL
    ws.frame({
      event: 'hitl_request', session_id: 's1',
      content: { tool_name: 'terminal', tool_args: { c: 'cmd1' }, description: '1', allowed_decisions: ['approve'] },
    });
    expect(onHitl).toHaveBeenCalledTimes(1);
    controller.sendHitlResponse!({ decision: 'approve' });

    // Second HITL
    ws.frame({
      event: 'hitl_request', session_id: 's1',
      content: { tool_name: 'terminal', tool_args: { c: 'cmd2' }, description: '2', allowed_decisions: ['approve'] },
    });
    expect(onHitl).toHaveBeenCalledTimes(2);

    controller.sendHitlResponse!({ decision: 'reject' });
    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });
});
