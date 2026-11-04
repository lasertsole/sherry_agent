/**
 * Structured question HITL (integration, backend mocked).
 *
 * The `question` tool can ask 1-4 questions in one dialog. This suite drives the
 * page through the SAME session socket handler it installs (`onHitl`) and pins:
 *
 * - the batch layout (one card per question, preview, custom answer, annotation
 *   box, unified submit / reject);
 * - the resume payload (one JSON `message` carrying every selection + note);
 * - the legacy single-question shape (a pending interrupt without `questions` /
 *   `_multi` keeps the historical immediate-submit behaviour);
 * - the per-question `allow_annotation: false` opt-out.
 */
import { describe, it, expect, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import sidPage from '@/pages/home/index/[sid].vue';

const socket = vi.hoisted(() => ({
  handlers: null as Record<string, (payload: unknown) => void> | null,
  sendHitlResponse: vi.fn(),
  stop: vi.fn(),
  holdFrames: vi.fn(),
  releaseFrames: vi.fn(),
  setSnapshotWatermark: vi.fn()
}));

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('vue-router', () => {
  const useRoute = () => ({ params: { sid: 'default' }, fullPath: '/home/default' });
  const useRouter = () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), go: vi.fn() });
  return {
    useRoute,
    useRouter,
    RouterLink: { name: 'RouterLink', props: ['to'], template: '<a><slot /></a>' },
    RouterView: { name: 'RouterView', template: '<div><slot /></div>' }
  };
});

vi.mock('@/composables/bridge/agent-socket', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/bridge/agent-socket')>();
  return {
    ...actual,
    acquireAgentSocket: () => ({
      setHandlers: (handlers: Record<string, (payload: unknown) => void>) => {
        socket.handlers = handlers;
      },
      sendHitlResponse: socket.sendHitlResponse,
      stop: socket.stop,
      holdFrames: socket.holdFrames,
      releaseFrames: socket.releaseFrames,
      setSnapshotWatermark: socket.setSnapshotWatermark,
      hasPendingSends: () => false,
      hasPendingMidStreamSends: () => false,
      settlePendingSends: () => {},
      settlePendingMidStreamSends: () => {},
      dispose: () => {},
      send: () => ({ controller: { abort: () => {} }, promise: Promise.resolve() }),
      sendCancelQueued: () => {},
      sendContinueGeneration: () => {},
      sendEditMessage: () => {},
      sendEditQueued: () => {},
      sendNow: () => {},
      sendTerminalBgKill: () => {}
    }),
    releaseAgentSocket: () => {}
  };
});

vi.mock('@/composables/db', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/db')>();
  return {
    ...actual,
    readCachedMessages: async () => [],
    cachedMaxTurnNum: async () => 0,
    cacheMessages: async () => {},
    clearCachedSession: async () => {},
    readCachedCharacter: async () => undefined,
    cacheCharacter: async () => {},
    clearCachedCharacter: async () => {},
    readCachedSessionMetaList: async () => [],
    cacheSessionMeta: async () => {},
    clearCachedSessionMeta: async () => {},
    saveSessionTitleOverride: async () => {},
    readSessionTitleOverrides: async () => new Map<string, string>(),
    clearSessionTitleOverride: async () => {},
    saveDraftTurn: async () => {},
    readDraftTurns: async () => [],
    clearDraftTurn: async () => {},
    clearDraftSession: async () => {}
  };
});

vi.mock('@/composables/requestApi', () => ({
  fetchApi: fetchApiMock,
  ensureGatewayToken: async () => null,
  withGatewayToken: (url: string) => url
}));

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>'
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' }
};

/** A batch payload: two questions, preview on the first option, annotation off on the second. */
function batchRequest() {
  return {
    tool_name: 'question',
    description: 'Pick the stack',
    allowed_decisions: ['approve', 'reject'],
    tool_args: {
      _multi: true,
      question: 'Which framework?',
      header: 'Framework',
      options: [
        { label: 'Vue', description: 'Vue 3', preview: '<template><App /></template>' },
        { label: 'React', description: 'React 19', preview: '' }
      ],
      multiple: false,
      questions: [
        {
          question: 'Which framework?',
          header: 'Framework',
          options: [
            { label: 'Vue', description: 'Vue 3', preview: '<template><App /></template>' },
            { label: 'React', description: 'React 19', preview: '' }
          ],
          multiple: false,
          allow_annotation: true
        },
        {
          question: 'Add SSR?',
          header: 'SSR',
          options: [
            { label: 'Yes', description: 'Nitro' },
            { label: 'No', description: 'SPA' }
          ],
          multiple: false,
          allow_annotation: false
        }
      ]
    }
  };
}

/** A legacy payload: no `questions`, no `_multi`. */
function legacyRequest() {
  return {
    tool_name: 'question',
    description: 'Which database?',
    allowed_decisions: ['approve', 'reject'],
    tool_args: {
      question: 'Which database?',
      header: 'DB choice',
      options: [
        { label: 'Postgres', description: 'Relational' },
        { label: 'SQLite', description: 'Embedded' }
      ],
      multiple: false
    }
  };
}

async function mountPage() {
  fetchApiMock.mockResolvedValue({ code: 200, data: null });
  const wrapper = mount(sidPage, { global: { stubs: primevueStub } });
  await flushPromises();
  return wrapper;
}

/**
 * Push one HITL request through the page's own socket handler.
 * @param wrapper
 * @param payload
 */
async function pushHitl(wrapper: ReturnType<typeof mount>, payload: unknown) {
  socket.handlers!.onHitl!(payload);
  await flushPromises();
  return wrapper;
}

describe('structured question HITL', () => {
  it('renders one card per question with previews and an annotation opt-out', async () => {
    const wrapper = await mountPage();
    await pushHitl(wrapper, batchRequest());

    const q0 = wrapper.get('[data-test="hitl-question-0"]');
    const q1 = wrapper.get('[data-test="hitl-question-1"]');
    expect(q0.text()).toContain('Framework');
    expect(q1.text()).toContain('SSR');

    // Preview renders as plain text inside the option card.
    expect(wrapper.get('[data-test="hitl-question-0-option-0"]').text()).toContain('<template><App /></template>');
    expect(wrapper.get('[data-test="hitl-question-0-option-1"]').find('pre').exists()).toBe(false);

    // allow_annotation=false hides only that question's box.
    expect(wrapper.find('[data-test="hitl-question-0-annotation"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="hitl-question-1-annotation"]').exists()).toBe(false);
    // Every question keeps its own custom-answer input.
    expect(wrapper.find('[data-test="hitl-question-1-custom"]').exists()).toBe(true);
  });

  it('submits every selection, custom text and annotation as one JSON message', async () => {
    const wrapper = await mountPage();
    await pushHitl(wrapper, batchRequest());
    socket.sendHitlResponse.mockClear();

    // Q0: pick Vue and leave a note; Q1: pick Yes and add a custom follow-up.
    await wrapper.get('[data-test="hitl-question-0-option-0"]').trigger('click');
    await wrapper.get('[data-test="hitl-question-0-annotation"]').setValue('needs Nitro');
    await wrapper.get('[data-test="hitl-question-1-option-0"]').trigger('click');
    await wrapper.get('[data-test="hitl-question-1-custom"]').setValue('also add tests');
    await wrapper.get('[data-test="hitl-multi-submit"]').trigger('click');
    await flushPromises();

    expect(socket.sendHitlResponse).toHaveBeenCalledTimes(1);
    const [body] = socket.sendHitlResponse.mock.calls[0] as [{ decision: string; message: string }];
    expect(body.decision).toBe('approve');
    const parsed = JSON.parse(body.message) as { answers: Array<Record<string, unknown>> };
    expect(parsed.answers).toEqual([
      {
        question: 'Which framework?',
        header: 'Framework',
        selection: ['Vue'],
        annotation: 'needs Nitro'
      },
      {
        question: 'Add SSR?',
        header: 'SSR',
        selection: ['Yes', 'also add tests'],
        annotation: ''
      }
    ]);
  });

  it('toggles options on a multi-select question', async () => {
    const wrapper = await mountPage();
    const payload = batchRequest();
    payload.tool_args.questions[1]!.multiple = true;
    await pushHitl(wrapper, payload);

    await wrapper.get('[data-test="hitl-question-1-option-0"]').trigger('click');
    await wrapper.get('[data-test="hitl-question-1-option-1"]').trigger('click');
    expect(wrapper.get('[data-test="hitl-question-1-option-0"]').classes().join(' ')).toContain('border-theme-main');
    expect(wrapper.get('[data-test="hitl-question-1-option-1"]').classes().join(' ')).toContain('border-theme-main');

    // Clicking the first one again deselects it.
    await wrapper.get('[data-test="hitl-question-1-option-0"]').trigger('click');
    expect(wrapper.get('[data-test="hitl-question-1-option-0"]').classes().join(' ')).not.toContain(
      'border-theme-main'
    );
  });

  it('rejects the whole batch with one reject decision', async () => {
    const wrapper = await mountPage();
    await pushHitl(wrapper, batchRequest());
    socket.sendHitlResponse.mockClear();

    await wrapper.get('[data-test="hitl-multi-reject"]').trigger('click');
    await flushPromises();

    expect(socket.sendHitlResponse).toHaveBeenCalledWith({ decision: 'reject', message: '' });
  });

  it('keeps a legacy single-question interrupt on the old path', async () => {
    const wrapper = await mountPage();
    await pushHitl(wrapper, legacyRequest());
    socket.sendHitlResponse.mockClear();

    // No batch cards; the historical option list renders.
    expect(wrapper.find('[data-test="hitl-question-0"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('DB choice');
    expect(wrapper.text()).toContain('Postgres');

    // Single-select submits immediately with the plain label (no JSON).
    const option = wrapper.findAll('button').find(button => button.text().includes('Postgres'));
    expect(option).toBeTruthy();
    await option!.trigger('click');
    await flushPromises();

    expect(socket.sendHitlResponse).toHaveBeenCalledWith({
      decision: 'approve',
      message: 'Postgres'
    });
  });
});
