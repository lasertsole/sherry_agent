/**
 * Meeting store (UI upgrade S4).
 *
 * The meeting backend (its own parent plan) is not shipped in this change set.
 * Per S4's fallback and S7's placeholder strategy, this store runs against a
 * deterministic mock so the list/page UI is real and navigable. Once
 * `/api/meetings` + WS land, set `useMock = false` — the action surface is the
 * same, only the transport changes.
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

export type MeetingStatus = 'in_progress' | 'waiting_user' | 'ended' | 'cancelled';

export interface MeetingSummary {
  id: string;
  topic: string;
  status: MeetingStatus;
  participant_count: number;
  flow_stage: string;
  iteration_count: number;
  created_at: number;
  updated_at: number;
  execution_plan?: MeetingExecutionPlan;
  final_summary?: string;
}

export interface MeetingMessage {
  id: number;
  meeting_id: string;
  sequence: number;
  speaker_kind: 'agent' | 'host' | 'user';
  speaker_id: string;
  speaker_name: string;
  message_role: 'normal' | 'summary' | 'inquiry';
  content: string;
  thinking_md?: string;
  is_partial: boolean;
  timestamp: number;
}

export interface MeetingPhase {
  id: string;
  allowed_agent_ids: string[];
  prerequisites: string[];
  completion_criteria: string;
}
export interface MeetingExecutionPlan {
  phases: MeetingPhase[];
}

const MOCK_MEETINGS: MeetingSummary[] = [
  {
    id: 'meeting-demo',
    topic: '架构评审',
    status: 'in_progress',
    participant_count: 3,
    flow_stage: 'discussion',
    iteration_count: 4,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_200
  }
];

const MOCK_MESSAGES: MeetingMessage[] = [
  {
    id: 1,
    meeting_id: 'meeting-demo',
    sequence: 1,
    speaker_kind: 'host',
    speaker_id: 'host',
    speaker_name: '主持人',
    message_role: 'normal',
    content: '今天我们讨论 **架构方案**。',
    is_partial: false,
    timestamp: 1_700_000_100
  },
  {
    id: 2,
    meeting_id: 'meeting-demo',
    sequence: 2,
    speaker_kind: 'agent',
    speaker_id: 'architect',
    speaker_name: '架构专家',
    message_role: 'summary',
    content: '建议采用分层架构。',
    thinking_md: '权衡单体与分层……',
    is_partial: false,
    timestamp: 1_700_000_150
  }
];

export const useMeetingStore = defineStore('meeting', () => {
  const meetings = ref<MeetingSummary[]>([]);
  const currentMeeting = ref<MeetingSummary | null>(null);
  const messages = ref<MeetingMessage[]>([]);
  const executionPlan = ref<MeetingExecutionPlan | null>(null);
  const useMock = ref(true);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const currentSpeakerId = ref<string | null>(null);
  const pendingQuestion = ref<string>('');
  const finalSummary = ref<string>('');

  const activeCount = computed(() => meetings.value.filter(m => m.status === 'in_progress').length);
  const orderedMessages = computed(() => [...messages.value].sort((a, b) => a.sequence - b.sequence));

  async function loadMeetings(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      if (useMock.value) {
        meetings.value = MOCK_MEETINGS.map(m => ({ ...m }));
      } else {
        const res = await fetchApiPayload<{ meetings: MeetingSummary[] }>({ url: '/meetings' });
        meetings.value = res?.meetings ?? [];
      }
    } catch (err) {
      error.value = String(err);
      meetings.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function createMeeting(topic: string, participantIds: string[]): Promise<MeetingSummary> {
    if (useMock.value) {
      const now = Math.floor(Date.now() / 1000);
      const meeting: MeetingSummary = {
        id: `meeting-${now}`,
        topic,
        status: 'in_progress',
        participant_count: participantIds.length,
        flow_stage: 'discussion',
        iteration_count: 0,
        created_at: now,
        updated_at: now
      };
      meetings.value = [meeting, ...meetings.value];
      return meeting;
    }
    // The backend seats participants by registered EXPERT NAME and needs a
    // session id; the app uses the same fixed 'default' session as the WS channel.
    const sessionId = 'default';
    const res = await fetchApiPayload<{ meeting: MeetingSummary }>({
      url: '/meetings',
      method: 'post',
      opts: { session_id: sessionId, topic, participants: participantIds }
    });
    await loadMeetings();
    return res!.meeting;
  }

  async function openMeeting(id: string): Promise<void> {
    if (useMock.value) {
      currentMeeting.value = meetings.value.find(m => m.id === id) ?? { ...MOCK_MEETINGS[0]!, id };
      messages.value = MOCK_MESSAGES.filter(m => m.meeting_id === 'meeting-demo' || m.meeting_id === id).map(m => ({
        ...m
      }));
      executionPlan.value = {
        phases: [
          { id: 'discussion', allowed_agent_ids: [], prerequisites: [], completion_criteria: '达成共识' },
          { id: 'conclusion', allowed_agent_ids: [], prerequisites: ['discussion'], completion_criteria: '产出结论' }
        ]
      };
      return;
    }
    const [detail, history] = await Promise.all([
      fetchApiPayload<{ meeting: MeetingSummary }>({ url: '/meetings/one', opts: { id } }),
      fetchApiPayload<{ messages: MeetingMessage[] }>({ url: '/meetings/messages', opts: { id } })
    ]);
    currentMeeting.value = detail!.meeting;
    messages.value = history!.messages;
    if (detail!.meeting.execution_plan) {
      executionPlan.value = { phases: detail!.meeting.execution_plan.phases ?? [] };
    }
  }

  function handleFrame(frame: Record<string, unknown>): void {
    switch (frame.event) {
      case 'meeting_speaker_changed':
        currentSpeakerId.value = String(frame.speaker_id ?? '');
        break;
      case 'meeting_message_delta': {
        const existing = messages.value.find(m => m.sequence === Number(frame.sequence));
        if (existing) existing.content += String(frame.delta ?? '');
        else {
          messages.value.push({
            id: messages.value.length + 1,
            meeting_id: String(frame.meeting_id ?? ''),
            sequence: Number(frame.sequence ?? 0),
            speaker_kind: 'agent',
            speaker_id: String(frame.speaker_id ?? ''),
            speaker_name: String(frame.speaker_name ?? ''),
            message_role: 'normal',
            content: String(frame.delta ?? ''),
            is_partial: true,
            timestamp: Date.now() / 1000
          });
        }
        break;
      }
      case 'meeting_message_completed': {
        const target = messages.value.find(m => m.sequence === Number(frame.sequence));
        if (target) target.is_partial = false;
        break;
      }
      case 'meeting_thinking_delta': {
        const target = messages.value.find(m => m.sequence === Number(frame.sequence));
        if (target) target.thinking_md = (target.thinking_md ?? '') + String(frame.delta ?? '');
        break;
      }
      case 'meeting_ask_user':
        pendingQuestion.value = String(frame.question ?? '');
        break;
      case 'meeting_summary_draft':
        finalSummary.value = String(frame.summary ?? '');
        break;
    }
  }

  async function interrupt(content: string): Promise<void> {
    pendingQuestion.value = '';
    if (useMock.value) {
      messages.value.push({
        id: messages.value.length + 1,
        meeting_id: currentMeeting.value?.id ?? '',
        sequence: Math.max(0, ...messages.value.map(m => m.sequence)) + 1,
        speaker_kind: 'user',
        speaker_id: 'user',
        speaker_name: '我',
        message_role: 'inquiry',
        content,
        is_partial: false,
        timestamp: Date.now() / 1000
      });
      return;
    }
    await fetchApiPayload({
      url: '/meetings/interrupt',
      method: 'post',
      opts: { meeting_id: currentMeeting.value?.id, content }
    });
    pendingQuestion.value = '';
  }

  async function confirmEnd(): Promise<void> {
    if (!currentMeeting.value) return;
    if (useMock.value) {
      currentMeeting.value.status = 'ended';
      meetings.value = meetings.value.map(m => (m.id === currentMeeting.value?.id ? { ...m, status: 'ended' } : m));
      return;
    }
    await fetchApiPayload({
      url: '/meetings/confirm-end',
      method: 'post',
      opts: { meeting_id: currentMeeting.value.id, summary: finalSummary.value || undefined }
    });
    await loadMeetings();
  }

  async function execute(): Promise<void> {
    if (!currentMeeting.value) return;
    if (!useMock.value) {
      await fetchApiPayload({
        url: '/meetings/execute',
        method: 'post',
        opts: { meeting_id: currentMeeting.value.id, session_id: 'default' }
      });
    }
  }

  function closeMeeting(): void {
    currentMeeting.value = null;
    messages.value = [];
    executionPlan.value = null;
    currentSpeakerId.value = null;
    pendingQuestion.value = '';
    finalSummary.value = '';
  }

  return {
    meetings,
    currentMeeting,
    messages,
    orderedMessages,
    executionPlan,
    currentSpeakerId,
    pendingQuestion,
    finalSummary,
    useMock,
    loading,
    error,
    activeCount,
    loadMeetings,
    createMeeting,
    openMeeting,
    handleFrame,
    interrupt,
    confirmEnd,
    execute,
    closeMeeting
  };
});
