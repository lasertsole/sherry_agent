/**
 * Meeting store (UI upgrade S4). Backend is not shipped yet, so the store is in
 * its documented mock mode; pin the action surface + WS frame reducers so the
 * later transport swap to `/api/meetings` stays behaviour-identical.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useMeetingStore } from '../meeting';

describe('stores/meeting — mock mode (S4)', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('loads meetings and derives the active count', async () => {
    const store = useMeetingStore();
    await store.loadMeetings();
    expect(store.meetings.length).toBeGreaterThan(0);
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
    expect(store.activeCount).toBe(store.meetings.filter(m => m.status === 'in_progress').length);
  });

  it('creates a meeting and prepends it to the list', async () => {
    const store = useMeetingStore();
    await store.loadMeetings();
    const before = store.meetings.length;
    const created = await store.createMeeting('新议题', ['a', 'b']);
    expect(created.topic).toBe('新议题');
    expect(created.participant_count).toBe(2);
    expect(store.meetings).toHaveLength(before + 1);
    expect(store.meetings[0]!.id).toBe(created.id);
  });

  it('opens a meeting with ordered messages and an execution plan', async () => {
    const store = useMeetingStore();
    await store.loadMeetings();
    await store.openMeeting('meeting-demo');
    expect(store.currentMeeting?.id).toBe('meeting-demo');
    expect(store.executionPlan?.phases.length).toBeGreaterThan(0);
    expect(store.orderedMessages.map(m => m.sequence)).toEqual([1, 2]);
  });

  it('reduces streaming WS frames: speaker, delta, completion, thinking', async () => {
    const store = useMeetingStore();
    await store.openMeeting('meeting-demo');

    store.handleFrame({ event: 'meeting_speaker_changed', speaker_id: 'architect' });
    expect(store.currentSpeakerId).toBe('architect');

    // Delta on an existing message appends; a new sequence inserts a partial one.
    store.handleFrame({ event: 'meeting_message_delta', sequence: 1, delta: ' 补充' });
    expect(store.orderedMessages[0]!.content).toContain(' 补充');
    store.handleFrame({
      event: 'meeting_message_delta',
      sequence: 3,
      delta: '新消息',
      meeting_id: 'meeting-demo',
      speaker_name: '专家'
    });
    const inserted = store.orderedMessages.find(m => m.sequence === 3);
    expect(inserted?.is_partial).toBe(true);
    expect(inserted?.content).toBe('新消息');

    store.handleFrame({ event: 'meeting_message_completed', sequence: 3 });
    expect(store.orderedMessages.find(m => m.sequence === 3)?.is_partial).toBe(false);

    store.handleFrame({ event: 'meeting_thinking_delta', sequence: 2, delta: '再想想' });
    expect(store.orderedMessages.find(m => m.sequence === 2)?.thinking_md).toContain('再想想');
  });

  it('interrupt appends a user inquiry; confirmEnd closes the meeting', async () => {
    const store = useMeetingStore();
    await store.openMeeting('meeting-demo');
    await store.interrupt('请补充成本分析');
    const userMsg = store.orderedMessages.at(-1)!;
    expect(userMsg.speaker_kind).toBe('user');
    expect(userMsg.message_role).toBe('inquiry');
    expect(userMsg.content).toBe('请补充成本分析');

    await store.confirmEnd();
    expect(store.currentMeeting?.status).toBe('ended');
  });

  it('reduces ask_user and summary_draft frames into visible state', async () => {
    const store = useMeetingStore();
    await store.openMeeting('meeting-demo');

    store.handleFrame({ event: 'meeting_ask_user', question: 'Use microservices?' });
    expect(store.pendingQuestion).toBe('Use microservices?');
    store.handleFrame({ event: 'meeting_summary_draft', summary: 'Final decision' });
    expect(store.finalSummary).toBe('Final decision');

    await store.interrupt('supplement');
    expect(store.pendingQuestion).toBe(''); // reply/question clears the pending ask

    store.closeMeeting();
    expect(store.pendingQuestion).toBe('');
    expect(store.finalSummary).toBe('');
  });

  it('closeMeeting clears all live state', async () => {
    const store = useMeetingStore();
    await store.openMeeting('meeting-demo');
    store.handleFrame({ event: 'meeting_speaker_changed', speaker_id: 'host' });
    store.closeMeeting();
    expect(store.currentMeeting).toBeNull();
    expect(store.messages).toEqual([]);
    expect(store.executionPlan).toBeNull();
    expect(store.currentSpeakerId).toBeNull();
  });
});
