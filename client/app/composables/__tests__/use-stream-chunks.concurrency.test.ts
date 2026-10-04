/**
 * Concurrent tool-call pairing (live-verified bug).
 *
 * One assistant message may declare N tool calls. The backend emits ALL N
 * `tool_start` frames during that message's own streaming (messages mode, driven
 * by `msg_chunk.tool_call_chunks`) and only afterwards their `tool_end` /
 * `tool_result` frames (updates mode, on the real ToolMessage) — so
 * `A.start → B.start → A.end` is the guaranteed wire order, not a rare race.
 *
 * Pairing by position therefore marked the LAST-DECLARED call done when an
 * EARLIER one finished: A span forever while B was completed before it ran, and
 * A's result text was written onto B. `tool_end`/`tool_result` must pair by
 * `toolId`; the positional lookup stays as the fallback for frames that carry no
 * id (history rows) and for the HITL resume path.
 */
import { describe, it, expect } from 'vitest';
import { ref } from 'vue';
import { useStreamChunks } from '../use-stream-chunks';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '../../pages/home/type';

const SID = 's1';

function setup() {
  const chatMessages = ref<MessageItem[]>([]);
  let nextId = -1;
  const slices = useStreamChunks(chatMessages, () => nextId--, {
    scheduleDraftWrite: () => {},
    commitDraftTurn: () => {},
    isDraftTurnActive: () => false
  });
  const tools = () => chatMessages.value.filter(m => m.role === CHAT_ROLE.TOOL);
  return { slices, tools, chatMessages };
}

describe('tool row pairing under concurrent tool calls', () => {
  it('records the tool-call id on the row so pairing is possible at all', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    expect(tools()[0]).toMatchObject({ toolId: 'call_A', toolStatus: 'running' });
  });

  it('completes the tool that finished, not the last one declared', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'write_file', 'tool_start', 1, { tool_id: 'call_B' });

    slices.appendStreamChunk(SID, 'read_file', 'tool_end', 1, { tool_id: 'call_A' });

    // A finished; B must still be running.
    expect(tools().map(t => t.toolStatus)).toEqual(['done', 'running']);
  });

  it('writes the result onto the row that produced it', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'write_file', 'tool_start', 1, { tool_id: 'call_B' });

    slices.appendStreamChunk(SID, 'contents of A', 'tool_result', 1, {
      tool_id: 'call_A',
      tool_name: 'read_file'
    });

    expect(tools()[0]?.toolResult).toBe('contents of A');
    expect(tools()[1]?.toolResult).toBeUndefined();
  });

  it('keeps both rows correct when the tools finish in declaration order', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'write_file', 'tool_start', 1, { tool_id: 'call_B' });

    slices.appendStreamChunk(SID, 'result A', 'tool_result', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'result B', 'tool_result', 1, { tool_id: 'call_B' });

    expect(tools().map(t => t.toolStatus)).toEqual(['done', 'done']);
    expect(tools().map(t => t.toolResult)).toEqual(['result A', 'result B']);
  });

  it('handles three concurrent calls finishing out of declaration order', () => {
    const { slices, tools } = setup();
    for (const id of ['call_A', 'call_B', 'call_C']) {
      slices.appendStreamChunk(SID, `tool_${id}`, 'tool_start', 1, { tool_id: id });
    }
    // C finishes first, then A, then B.
    slices.appendStreamChunk(SID, 'rC', 'tool_result', 1, { tool_id: 'call_C' });
    slices.appendStreamChunk(SID, 'rA', 'tool_result', 1, { tool_id: 'call_A' });

    expect(tools().map(t => t.toolResult)).toEqual(['rA', undefined, 'rC']);

    slices.appendStreamChunk(SID, 'rB', 'tool_result', 1, { tool_id: 'call_B' });
    expect(tools().map(t => t.toolResult)).toEqual(['rA', 'rB', 'rC']);
  });

  it('propagates the error flag onto the matching row only', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'write_file', 'tool_start', 1, { tool_id: 'call_B' });

    slices.appendStreamChunk(SID, 'boom', 'tool_result', 1, { tool_id: 'call_B', error: true });

    expect(tools().map(t => t.toolStatus)).toEqual(['running', 'error']);
  });

  it('still pairs by position when the frames carry no tool id', () => {
    // The single-tool turn is the legacy shape: no id on either frame, so the
    // positional fallback must keep working (history rows never carry an id).
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1);
    slices.appendStreamChunk(SID, 'ok', 'tool_result', 1, { tool_name: 'read_file' });

    expect(tools()[0]).toMatchObject({ toolStatus: 'done', toolResult: 'ok' });
  });

  it('still lands a HITL resume result on the interrupted card of the previous turn', () => {
    // The approve/reject card was created in turn 1; the resume turn is 2 and its
    // tool_id matches no same-turn row, so the cascade falls through to the last
    // unsettled card.
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'patch_file', 'tool_start', 1, { tool_id: 'call_A' });

    slices.appendStreamChunk(SID, 'patched', 'tool_result', 2, { tool_id: 'call_A' });

    expect(tools()[0]).toMatchObject({ toolName: 'patch_file', toolStatus: 'done', toolResult: 'patched' });
  });

  it('leaves an unrelated turn untouched', () => {
    const { slices, tools } = setup();
    slices.appendStreamChunk(SID, 'read_file', 'tool_start', 1, { tool_id: 'call_A' });
    slices.appendStreamChunk(SID, 'write_file', 'tool_start', 2, { tool_id: 'call_B' });

    slices.appendStreamChunk(SID, 'done A', 'tool_result', 1, { tool_id: 'call_A' });

    expect(tools().map(t => [t.turn_num, t.toolStatus, t.toolResult])).toEqual([
      [1, 'done', 'done A'],
      [2, 'running', undefined]
    ]);
  });
});
