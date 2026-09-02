import { describe, expect, it } from 'vitest';
import { toMessageItems } from '../messageItems';
import { CHAT_ROLE } from '../type';
import type { CachedMessage } from '@/composables/db';

/**
 * Minimal valid CachedMessage row (mirrors the backend history API row shape; see db.ts).
 * `origin` is intentionally absent from the base object: legacy cached rows never carried it.
 */
const row = (over: Partial<CachedMessage>): CachedMessage => ({
  id: 1,
  turn_num: 1,
  session_id: 'default',
  role: 'human',
  content: 'hello',
  timestamp: '20260902120000',
  images: null,
  audios: null,
  videos: null,
  tool_call_id: null,
  tool_calls: null,
  tool_status: null,
  tool_name: null,
  finish_reason: null,
  reasoning: null,
  reasoning_content: null,
  model_name: null,
  input_tokens: null,
  output_tokens: null,
  ...over
});

describe('toMessageItems origin mapping (subagent-origin-tagging Task 5)', () => {
  it('maps a row with origin="subagent_completion" onto item.origin', () => {
    const items = toMessageItems([
      row({
        id: 11,
        origin: 'subagent_completion',
        content: '[subagent:研究员 done]\n任务已完成'
      })
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]!.origin).toBe('subagent_completion');
    // The carrier is a human-role row: the render layer branches on (role=USER && origin)
    expect(items[0]!.role).toBe(CHAT_ROLE.USER);
    expect(items[0]!.content).toBe('[subagent:研究员 done]\n任务已完成');
  });

  it('leaves origin undefined for a legacy row without the origin field', () => {
    // Legacy cached rows (pre-origin backend) have no origin key at all
    const items = toMessageItems([row({ id: 12 })]);
    expect(items).toHaveLength(1);
    expect(items[0]!.origin).toBeUndefined();
  });

  it('normalizes a null origin (backend TEXT NULL) to undefined', () => {
    const items = toMessageItems([row({ id: 13, origin: null })]);
    expect(items[0]!.origin).toBeUndefined();
  });
});
