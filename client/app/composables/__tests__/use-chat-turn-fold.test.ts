import { describe, it, expect } from 'vitest';
import { classifyTurnFold } from '../use-chat-turn-fold';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.AI,
  content: 'x',
  id: 1,
  turn_num: 0,
  timestamp: 't',
  ...over
});

describe('classifyTurnFold', () => {
  it('folds the work before a settled final answer', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, content: 'let me check' }),
      msg({ id: 2, role: CHAT_ROLE.TOOL, toolName: 'read_file', toolDurationMs: 1200 }),
      msg({ id: 3, content: 'final answer', reasoning: 'thought' })
    ]);

    expect(fold.eligible).toBe(true);
    expect(fold.key).toBe(1);
    expect(fold.history.map(m => m.id)).toEqual([1, 2]);
    expect(fold.finalMessage?.id).toBe(3);
    expect(fold.toolCount).toBe(1);
    expect(fold.durationMs).toBe(1200);
    expect(fold.hasReasoning).toBe(true);
  });

  it('reports an unknown tool duration as null, never as 0ms', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, role: CHAT_ROLE.TOOL, toolName: 'read_file' }),
      msg({ id: 2, content: 'answer' })
    ]);
    expect(fold.toolCount).toBe(1);
    expect(fold.durationMs).toBeNull();
  });

  it('does not fold a streaming final answer', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, role: CHAT_ROLE.TOOL, toolName: 'read_file' }),
      msg({ id: 2, content: 'partial', streaming: true })
    ]);
    expect(fold.eligible).toBe(false);
  });

  it('does not fold a turn that ended on a tool call', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, content: 'working' }),
      msg({ id: 2, role: CHAT_ROLE.TOOL, toolName: 'terminal', toolStatus: 'running' })
    ]);
    expect(fold.eligible).toBe(false);
  });

  it('does not fold while a tool card is still running', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, content: 'working' }),
      msg({ id: 2, role: CHAT_ROLE.TOOL, toolName: 'terminal', toolStatus: 'running' }),
      msg({ id: 3, content: 'answer' })
    ]);
    expect(fold.eligible).toBe(false);
  });

  it('does not treat a tool-call-bearing AI row as the final answer', () => {
    const fold = classifyTurnFold([msg({ id: 1, content: 'calling a tool', hasToolCalls: true })]);
    expect(fold.eligible).toBe(false);
  });

  it('folds a lone answer that carries reasoning', () => {
    const fold = classifyTurnFold([msg({ id: 1, content: 'answer', reasoning: 'thought' })]);
    expect(fold.eligible).toBe(true);
    expect(fold.history).toEqual([]);
    expect(fold.hasReasoning).toBe(true);
  });

  it('leaves a lone plain answer linear', () => {
    expect(classifyTurnFold([msg({ id: 1, content: 'answer' })]).eligible).toBe(false);
  });

  it('treats a media-only final answer as visible content', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, role: CHAT_ROLE.TOOL, toolName: 'text_to_image' }),
      msg({ id: 2, content: '', images: ['a.png'] })
    ]);
    expect(fold.eligible).toBe(true);
  });

  it('ignores injected carrier rows defensively', () => {
    const fold = classifyTurnFold([
      msg({ id: 1, role: CHAT_ROLE.USER, origin: 'project_dir', content: 'notice' }),
      msg({ id: 2, content: 'answer' })
    ]);
    expect(fold.eligible).toBe(false);
  });
});
