import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ChatBox from '@/pages/home/components/ChatBox.vue';
import sidPage from '@/pages/home/index/[sid].vue';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';
import type { CachedMessage } from '@/composables/db';

/**
 * Image rendering integration tests (backend mocked).
 *
 * Covers the two code paths involved in this media-rendering fix:
 *  1. ChatBox.vue's resolveImageSrc — turns a message's `images` into a renderable <img src>:
 *       - user messages (base64, without a data: prefix) -> `data:image/*;base64,<origin>`
 *       - AI messages (absolute path of a persisted file) -> `{BACKEND}/media?session_id=<sid>&filename=<basename>`
 *  2. home/index/[sid].vue's loadSessionHistory — fetches server history rows (CachedMessage)
 *     through get_history_by_turn_page and passes their `images` through to the ChatBox messages prop.
 *
 * ChatBox is mounted as a real Vue component (not stubbed); the backend URL is provided by
 * `VITE_API_BACK_URL=http://localhost:8080` from vitest.integration.config.ts, which covers
 * exactly the file-path branch of resolveImageSrc.
 *
 * [sid].vue is mounted directly: the per-session chat page owns the pass-through logic since
 * the shell/home split (home/index.vue only hosts SessionSidebar + nested NuxtPage now).
 * Its setup runs an immediate `watch(sessionId)` that kicks loadSessionHistory ->
 * get_history_by_turn_page -> fetchApi, so the backend is seeded at the fetchApi transport
 * level. Dexie has no IndexedDB in happy-dom (every operation rejects with MissingAPIError),
 * so the persistence wrappers are neutralized via vi.mock. vue-router is explicitly imported
 * by [sid].vue and mocked here; SubagentTasksView (which drags in @antv/g6) is replaced by a
 * dummy module to keep the graph light.
 */

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

vi.mock('@/pages/home/components/SubagentTasksView.vue', () => ({
  default: { name: 'SubagentTasksView', template: '<div class="stv-stub"></div>' }
}));

const base = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'hello',
  id: 1,
  turn_num: 0,
  timestamp: '20260621004725',
  ...over
});

describe('ChatBox.vue image rendering (integration, backend mocked)', () => {
  it('renders a user base64 image with the data:image/*;base64, prefix', () => {
    const b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [b64], content: '' })]
      }
    });
    // The <img> inside the image container (class w-24, as opposed to the avatar's w-full)
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    expect(img.attributes('src')).toBe(`data:image/*;base64,${b64}`);
  });

  it('renders an AI file-path image via the backend /media URL', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 2,
            role: CHAT_ROLE.AI,
            session_id: 'default',
            images: ['C:/project/src/main/media/12345_67890.png'],
            // Note: an AI message with empty content gets filtered out by filteredMessages
            // as an "empty placeholder", which also suppresses the image branch —
            // non-empty content is required to hit the normal rendering path.
            content: 'look at this'
          })
        ]
      }
    });
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    // backendBaseUrl comes from VITE_API_BACK_URL=http://localhost:8080
    expect(img.attributes('src')).toBe('http://localhost:8080/media?session_id=default&filename=12345_67890.png');
  });

  it('renders no image container for a message without images', () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'plain' })] }
    });
    expect(wrapper.find('img.w-24').exists()).toBe(false);
  });

  it('renders multiple images in one message, each resolved independently', () => {
    const b1 = 'AAAA';
    const b2 = 'BBBB';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [b1, b2], content: '' })]
      }
    });
    const imgs = wrapper.findAll('img.w-24');
    expect(imgs).toHaveLength(2);
    expect(imgs[0].attributes('src')).toBe(`data:image/*;base64,${b1}`);
    expect(imgs[1].attributes('src')).toBe(`data:image/*;base64,${b2}`);
  });

  it('passes an explicit absolute-url image through unchanged (no /media misroute)', () => {
    const url = 'http://127.0.0.1:8080/images/abc123.png';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [url], content: '' })]
      }
    });
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    // An absolute URL must be passed through unchanged, not misrouted by isFilePath(.png) into a /media request
    expect(img.attributes('src')).toBe(url);
  });

  it('falls back to served URLs parsed from the content Location marker when images is empty', () => {
    const url = 'http://127.0.0.1:8080/images/abc123.png';
    const con = `[System: The user uploaded 1 image(s). Location: ${url}. If you need to view the image(s), use the image_to_text skill.]`;
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [], content: con })]
      }
    });
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    expect(img.attributes('src')).toBe(url);
  });
});

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>'
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' }
};

describe('home/index/[sid].vue image pass-through (integration, backend mocked)', () => {
  // Server history rows (CachedMessage) seeded at the fetchApi transport level:
  // the real get_history_by_turn_page (messages.ts) runs, including its
  // Array.isArray compatibility branch, mergeDedup and toMessageItems mapping.
  const rows: CachedMessage[] = [
    {
      session_id: 'default',
      role: CHAT_ROLE.USER,
      content: 'with image',
      images: ['aGVsbG8='],
      id: 1,
      turn_num: 0,
      timestamp: '20260621004725',
      tool_call_id: null,
      tool_calls: null,
      tool_status: null,
      tool_name: null,
      finish_reason: null,
      reasoning: null,
      reasoning_content: null
    }
  ];

  beforeEach(() => {
    vi.stubGlobal(
      'fetchApi',
      vi.fn(async (opts?: { url?: string }) =>
        opts?.url === '/get_history_by_turn_page' ? rows : { code: 200, data: null }
      )
    );
  });

  it('passes `images` from server history rows into ChatBox messages and renders them', async () => {
    const wrapper = mount(sidPage, { global: { stubs: primevueStub } });
    // setup's immediate watch(sessionId) kicks loadSessionHistory; wait for the
    // fetchApi -> toMessageItems -> chatMessages chain and the re-render to settle.
    await flushPromises();
    await flushPromises();

    // ChatBox is a real component (not stubbed) -> once passed through, an <img> should actually render
    const chatBox = wrapper.findComponent(ChatBox);
    expect(chatBox.exists()).toBe(true);
    const messages = chatBox.props('messages') as MessageItem[];
    expect(messages).toHaveLength(1);
    expect(messages[0].images).toEqual(['aGVsbG8=']);

    // End to end: the message was passed through and rendered by ChatBox.resolveImageSrc into a base64 data URL
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    expect(img.attributes('src')).toBe('data:image/*;base64,aGVsbG8=');
  });
});
