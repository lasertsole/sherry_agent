import { describe, it, expect } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ChatBox from '@/pages/home/components/ChatBox.vue';
import { DEFAULT_PLACEHOLDER_AVATAR } from '~/composables/defaultCharacter';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';
import { TURN_SCRUBBER_PAGE_SIZE } from '@/composables/use-chat-virtual-list';
import { useImagePreview } from '@/composables/useImagePreview';

const base = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'hello',
  id: 1,
  turn_num: 0,
  timestamp: '20260621004725',
  ...over
});

describe('ChatBox.vue (integration, backend mocked)', () => {
  it('renders a single AI message with the neutral default name and sanitized markdown', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.AI, content: '**bold** world' })]
      }
    });
    // No character name on the prop → the neutral default label (改自 橘雪莉:
    // the persona name must never stand in for a nameless preset).
    expect(wrapper.text()).toContain('雪莉');
    expect(wrapper.text()).toContain('world');
    // markdown-it turns **bold** into <strong> inside the sanitized v-html
    expect(wrapper.find('.w-fit').html()).toContain('<strong>bold</strong>');
  });

  it('credits human messages as 我', () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'hi' })] }
    });
    expect(wrapper.text()).toContain('我');
  });

  it('filters out TOOL role messages entirely', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.TOOL, content: 'tool-call' }),
          base({ id: 2, role: CHAT_ROLE.USER, content: '被保留' })
        ]
      }
    });
    const text = wrapper.text();
    expect(text).not.toContain('tool-call');
    expect(text).toContain('被保留');
  });

  it('renders the tool call card for TOOL messages', async () => {
    // TOOL-role messages render a dedicated tool card (toolName + status icon);
    // AI messages carrying a tool_calls field render no indicator block anymore.
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 1,
            role: CHAT_ROLE.TOOL,
            content: '',
            toolName: 'web_search',
            toolStatus: 'running'
          })
        ]
      }
    });
    expect(wrapper.text()).toContain('web_search');
    // Expanding the card reveals the live "running" hint (chatBox.toolRunning).
    await wrapper.find('.pi-hammer').trigger('click');
    expect(wrapper.text()).toContain('执行中…');
  });

  it('reveals a tool card on demand for the toolbar\u2019s running-command entry', async () => {
    // The toolbar entry hands a running command's message id to this method: the
    // card is where the command's terminal output streams, so it must expand and
    // be ringed briefly (collapsed by default).
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.AI, content: 'running it' }),
          base({
            id: 2,
            role: CHAT_ROLE.TOOL,
            content: '',
            toolName: 'terminal',
            toolStatus: 'running',
            toolArgs: { command: 'npm run build' }
          })
        ]
      }
    });
    expect(wrapper.find('.ring-theme-main\\/50').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('npm run build');

    (wrapper.vm as unknown as { scrollToMessage: (id: number) => void }).scrollToMessage(2);
    await flushPromises();

    expect(wrapper.text()).toContain('npm run build');
    expect(wrapper.find('.ring-theme-main\\/50').exists()).toBe(true);
  });

  it('ignores a message id that is not in the loaded window', async () => {
    const wrapper = mount(ChatBox, { props: { messages: [base({ id: 1, content: 'hi' })] } });

    (wrapper.vm as unknown as { scrollToMessage: (id: number) => void }).scrollToMessage(999);
    await flushPromises();

    expect(wrapper.text()).toContain('hi');
    expect(wrapper.find('.ring-theme-main\\/50').exists()).toBe(false);
  });

  it('strips <script> tags from user content via markdown-it + DOMPurify', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, content: '<script>alert(1)</script>hi' })]
      }
    });
    const html = wrapper.find('.w-fit').html();
    const text = wrapper.text();
    // DOMPurify removes the `<script>` ELEMENT entirely (verified above), but
    // per the spec it is allowed to keep the script's inner text as inert text.
    // So the markdown-rendered sibling text must survive.
    expect(text).toContain('hi');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('</script>');
  });

  it('hides the avatar on a consecutive same-role message', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.USER, content: 'first' }),
          base({ id: 2, role: CHAT_ROLE.USER, content: 'second' })
        ]
      }
    });
    // 2 avatars exist (one per message), rendered as images — an empty avatar prop
    // falls back to the neutral placeholder silhouette, never a bare glyph.
    const slots = wrapper.findAll('div[class*="w-10"][class*="rounded-full"]');
    expect(slots).toHaveLength(2);
    const avatars = slots.map(slot => slot.find('img'));
    expect(avatars.every(a => a.exists())).toBe(true);
    expect(avatars[0]!.attributes('src')).toBe(DEFAULT_PLACEHOLDER_AVATAR);
    // first avatar visible, second hidden (previous message shares the role)
    expect(avatars[0]!.classes()).not.toContain('hidden');
    expect(avatars[1]!.classes()).toContain('hidden');
    // The hidden slot keeps its 40px but goes fully transparent: the placeholder disc
    // would otherwise show as an empty grey circle next to every consecutive message.
    expect(slots[0]!.classes()).toContain('bg-gray-100');
    expect(slots[1]!.classes()).toContain('opacity-0');
    expect(slots[1]!.classes()).not.toContain('bg-gray-100');
    expect(slots[1]!.attributes('aria-hidden')).toBe('true');
    expect(slots[0]!.attributes('aria-hidden')).toBeUndefined();
    // Layout is untouched: both slots still measure the same width.
    expect(slots[1]!.classes()).toContain('w-10');
  });

  it('renders no empty bubble for a reasoning-only AI turn', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.USER, content: '第11轮' }),
          base({ id: 2, role: CHAT_ROLE.AI, content: '   ', reasoning: '先想一下……' })
        ]
      }
    });

    // The thinking block is the only body of that turn: no white content bubble
    // (`w-fit` + `text-gray-900` is the AI bubble's signature), and the user bubble
    // next to it is untouched.
    expect(wrapper.text()).toContain('思考过程');
    expect(wrapper.findAll('.w-fit.text-gray-900')).toHaveLength(0);
    expect(wrapper.findAll('.w-fit')).toHaveLength(2); // thinking block + user bubble
    expect(wrapper.text()).toContain('第11轮');
  });

  it('keeps the bubble for an attachment-only message', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, content: '', images: ['a.png'] })]
      }
    });

    // The bubble survives for its attachment (no thinking block on it).
    expect(wrapper.findAll('.w-fit')).toHaveLength(1);
    expect(wrapper.find('img').exists()).toBe(true);
  });

  it('shows empty state when no messages', () => {
    const wrapper = mount(ChatBox, { props: { messages: [] } });
    expect(wrapper.find('.flex-1').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('橘雪莉');
  });

  it('lazy-loads history media attachments', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 31,
            role: CHAT_ROLE.AI,
            content: 'media message',
            images: ['iVBORw0KGgoAAAANSUhEUg=='],
            audios: ['QUJDRA=='],
            videos: ['RUZHSA==']
          })
        ]
      }
    });

    // Images decode off the main thread and only when near the viewport.
    const image = wrapper.find('img[src^="data:image"]');
    expect(image.exists()).toBe(true);
    expect(image.attributes('loading')).toBe('lazy');
    expect(image.attributes('decoding')).toBe('async');

    // Audio/video download nothing until the user presses play.
    expect(wrapper.find('audio').attributes('preload')).toBe('none');
    expect(wrapper.find('video').attributes('preload')).toBe('none');
  });

  it('keeps the image preview interaction intact for lazy images', async () => {
    const { previewSrc, isPreviewVisible, closePreview } = useImagePreview();
    closePreview();
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 32,
            role: CHAT_ROLE.AI,
            content: 'media message',
            images: ['iVBORw0KGgoAAAANSUhEUg==']
          })
        ]
      }
    });
    await wrapper.find('img[src^="data:image"]').trigger('click');
    expect(isPreviewVisible.value).toBe(true);
    expect(previewSrc.value).toBe('data:image/*;base64,iVBORw0KGgoAAAANSUhEUg==');
    closePreview();
  });
});

// ── Background-task system card (subagent-origin-tagging Task 5) ─────────────
// Backend history rows whose origin column is "subagent_completion" are background-task
// completion carriers (USER-role rows). They must render as a centered, muted system
// card — never as a user bubble. The carrier's first line "[subagent:<name> <status>]"
// is self-describing and shown verbatim (no parsing).

/** Realistic carrier content (matches agent/tools/subagent/announce/completion_message.py format) */
const CARRIER = '[subagent:研究员 done]\n后台检索已完成，结果已送达主会话。';

describe('ChatBox floating layer (integration, backend mocked)', () => {
  it('clips its overlays at the column edge (overflow-hidden root)', () => {
    // Both sidebars open narrow the chat column below the progress float's 320px
    // panel (measured: 280px column, 52px spill over the session list), so every
    // overlay anchored here is clipped at the column boundary instead of spilling.
    // (find, not wrapper.classes(): the template's leading comment makes the root
    // a fragment, so wrapper.element is not the styled div.)
    const wrapper = mount(ChatBox, { props: { messages: [base({ id: 1, content: 'hi' })] } });
    const root = wrapper.find('div.relative.overflow-hidden');
    expect(root.exists()).toBe(true);
    expect(root.classes()).toContain('min-h-0');
  });
});

describe('ChatBox turn scrubber (integration, backend mocked)', () => {
  it('offers a page of marks and pages through the rest of the session', () => {
    const messages = Array.from({ length: 25 }, (_, i) => [
      base({ id: i * 2 + 1, role: CHAT_ROLE.USER, turn_num: i + 1, content: `第 ${i + 1} 条` }),
      base({ id: i * 2 + 2, role: CHAT_ROLE.AI, turn_num: i + 1, content: '答' })
    ]).flat();

    const wrapper = mount(ChatBox, { props: { messages } });

    // 25 turns, one page of 20 drawn (plus the two page arrows).
    const arrows = wrapper.findAll('[aria-label="更新的消息"]');
    expect(arrows).toHaveLength(1);
    // The bars carry `.group`; the page arrows do not.
    const marks = wrapper.findAll('nav[aria-label="历史消息穿梭器"] button.group');
    expect(marks).toHaveLength(TURN_SCRUBBER_PAGE_SIZE);
    expect(marks[0]!.attributes('aria-label')).toBe('跳到第 1 轮');
    // The arrows reach the rest of the session.
    expect(wrapper.find('[aria-label="更早的消息"]').attributes('disabled')).toBeDefined();
  });

  it('does not render the scrubber for a single-turn session', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.USER, turn_num: 1, content: 'hi' }),
          base({ id: 2, role: CHAT_ROLE.AI, turn_num: 1, content: 'hello' })
        ]
      }
    });

    expect(wrapper.find('nav[aria-label="历史消息穿梭器"]').exists()).toBe(false);
  });
});

describe('ChatBox background-task system card (integration, backend mocked)', () => {
  it('renders a USER message with origin as a centered muted system card, collapsed by default', async () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 21, content: CARRIER, origin: 'subagent_completion' })]
      }
    });
    // Card marker class present
    const card = wrapper.find('.background-task-card');
    expect(card.exists()).toBe(true);
    // Muted label from chat.backgroundMessage (integration stub resolves zh locale)
    expect(wrapper.text()).toContain('后台任务');
    // COLLAPSED by default: the header only; the announcement body is not rendered
    expect(wrapper.text()).not.toContain('[subagent:研究员 done]');
    expect(wrapper.text()).not.toContain('后台检索已完成，结果已送达主会话。');
    // The header IS the toggle affordance
    const header = card.find('button');
    expect(header.exists()).toBe(true);
    expect(header.attributes('aria-expanded')).toBe('false');

    await header.trigger('click');

    // Expanded: the carrier text shows verbatim (the [subagent:...] first line is NOT parsed away)
    expect(wrapper.text()).toContain('[subagent:研究员 done]');
    expect(wrapper.text()).toContain('后台检索已完成，结果已送达主会话。');
    expect(header.attributes('aria-expanded')).toBe('true');

    // Toggling again collapses it back
    await header.trigger('click');
    expect(wrapper.text()).not.toContain('后台检索已完成，结果已送达主会话。');

    // User-bubble markup absent: no blue bubble, no right-reversed row flow
    expect(wrapper.html()).not.toContain('bg-[#2563EB]');
    expect(wrapper.html()).not.toContain('flex-row-reverse');
  });

  it('renders a HUMAN row with origin as the same card — never as a user bubble', async () => {
    // The workspace notices are HumanMessages tagged with their origin (the
    // injected-carrier role), so a real user bubble must never be drawn for one.
    const notice =
      '[项目目录已切换 / working directory changed] The working directory moved from `/tmp/a` to `/tmp/b`.';
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 31, content: notice, origin: 'project_dir' })] }
    });

    const card = wrapper.find('.background-task-card');
    expect(card.exists()).toBe(true);
    // The per-origin label names the source (zh stub: 项目目录切换, not 后台任务)
    expect(wrapper.text()).toContain('项目目录切换');
    expect(wrapper.text()).not.toContain('后台任务');
    // Collapsed by default; expanding shows the verbatim notice
    expect(wrapper.text()).not.toContain('working directory moved');
    await card.find('button').trigger('click');
    expect(wrapper.text()).toContain('working directory moved');
    // No user bubble: no blue bubble and no right-reversed row flow
    expect(wrapper.html()).not.toContain('bg-[#2563EB]');
    expect(wrapper.html()).not.toContain('flex-row-reverse');
  });

  it('renders the git-branch notice as its own card, labelled by its origin', async () => {
    const notice =
      '[Git 分支/HEAD 已变化 / git branch changed] The repository at `/tmp/a` moved from `main@1a2b3c4d` to `dev@5e6f7a8b`.';
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 32, content: notice, origin: 'git_head' })] }
    });

    expect(wrapper.find('.background-task-card').exists()).toBe(true);
    expect(wrapper.text()).toContain('Git 分支切换');
    expect(wrapper.text()).not.toContain('项目目录切换');
    expect(wrapper.html()).not.toContain('bg-[#2563EB]');
    expect(wrapper.text()).not.toContain('dev@5e6f7a8b');
    await wrapper.find('.background-task-card button').trigger('click');
    expect(wrapper.text()).toContain('dev@5e6f7a8b');
  });

  it('renders an AI row with origin as the same card — never as the assistant reply', async () => {
    // Legacy shape: rows stored while the notice was still an AIMessage keep
    // rendering as cards instead of looking like the assistant's answer.
    const notice =
      '[项目目录已切换 / working directory changed] The working directory moved from `/tmp/a` to `/tmp/b`.';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 33, role: CHAT_ROLE.AI, content: notice, origin: 'project_dir' })]
      }
    });

    const card = wrapper.find('.background-task-card');
    expect(card.exists()).toBe(true);
    // The per-origin label names the source (zh stub: 项目目录切换, not 后台任务)
    expect(wrapper.text()).toContain('项目目录切换');
    expect(wrapper.text()).not.toContain('后台任务');
    // Collapsed by default; expanding shows the verbatim notice
    await card.find('button').trigger('click');
    expect(wrapper.text()).toContain('working directory moved');
    // No assistant bubble: the AI display name/avatar is not attached to it
    expect(wrapper.text()).not.toContain('橘雪莉');
    expect(wrapper.find('.w-fit').exists()).toBe(false);
  });

  it('keeps the legacy user bubble for a USER message without origin', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 22, content: '真实的用户消息' })]
      }
    });
    // No system card rendered for legacy rows
    expect(wrapper.find('.background-task-card').exists()).toBe(false);
    // The existing user bubble branch is untouched: blue right-side bubble still renders
    expect(wrapper.html()).toContain('bg-[#2563EB]');
    expect(wrapper.text()).toContain('真实的用户消息');
    // The system-card label never leaks into the legacy branch
    expect(wrapper.text()).not.toContain('后台任务');
  });

  it.each([
    ['quality_gate', '质量门控'],
    ['task_intent', '任务意图'],
    ['todo_continuation', '待办提醒'],
    ['something_new', '系统消息']
  ])('labels the neutral card with its own source (%s)', (origin, label) => {
    // Every injector tags its rows with an origin, and the card names that
    // source instead of the generic background-task label. The generic-origin
    // card (系统消息) collapses by default exactly like the tagged ones.
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 24, content: '门控正文', origin })] }
    });

    expect(wrapper.find('.background-task-card').exists()).toBe(true);
    expect(wrapper.text()).toContain(label);
    expect(wrapper.find('.background-task-card button').attributes('aria-expanded')).toBe('false');
    expect(wrapper.text()).not.toContain('门控正文');
    expect(wrapper.html()).not.toContain('bg-[#2563EB]');
  });

  it('does not treat a null/empty origin as a background task', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 23, content: 'null origin 的用户消息', origin: undefined })]
      }
    });
    expect(wrapper.find('.background-task-card').exists()).toBe(false);
    expect(wrapper.html()).toContain('bg-[#2563EB]');
  });
});

describe('ChatBox scroll-up history hooks (integration, backend mocked)', () => {
  it('shows the older-history loading pill only while a page request runs', async () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'hi' })], loadingOlder: true }
    });
    expect(wrapper.text()).toContain('正在加载更早的消息');

    await wrapper.setProps({ loadingOlder: false });
    expect(wrapper.text()).not.toContain('正在加载更早的消息');
  });

  it('emits reach-top when the list is scrolled to the top', async () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'hi' })] }
    });
    const el = wrapper.find('.overflow-auto').element as HTMLElement;
    Object.defineProperty(el, 'scrollHeight', { value: 5000, configurable: true });
    Object.defineProperty(el, 'clientHeight', { value: 500, configurable: true });
    el.scrollTop = 0;
    await wrapper.find('.overflow-auto').trigger('scroll');
    expect(wrapper.emitted('reach-top')).toHaveLength(1);
  });
});

describe('ChatBox streaming bubble (integration, backend mocked)', () => {
  it('renders only the tail of a very long streaming answer, then the full text when settled', async () => {
    // A single multi-thousand-line row re-laid-out on every flush starved the
    // page (menus, timers and fetches froze for tens of seconds on a 2000-line
    // answer); while the row streams only its tail is rendered.
    const head = Array.from({ length: 400 }, (_, i) => `head-${i}`).join('\n');
    const tailLines = Array.from({ length: 400 }, (_, i) => `tail-${i}`).join('\n');
    const long = `${head}\n${tailLines}`;
    const streaming = base({ id: 60, role: CHAT_ROLE.AI, content: long, streaming: true });

    const wrapper = mount(ChatBox, { props: { messages: [streaming] } });
    // The hint explains the preview, the tail is rendered, the head is not.
    expect(wrapper.text()).toContain('仅显示末尾内容');
    expect(wrapper.text()).toContain('tail-399');
    expect(wrapper.text()).not.toContain('head-0');
    expect(wrapper.text()).not.toContain('tail-0');

    // The turn settles → the row collapses to a head preview with an expand
    // control (a window usually holds several multi-thousand-line rows; letting
    // every settled answer render in full is what starved the page).
    await wrapper.setProps({ messages: [{ ...streaming, streaming: false }] });
    expect(wrapper.text()).toContain('head-0');
    expect(wrapper.text()).not.toContain('tail-0');
    expect(wrapper.text()).not.toContain('仅显示末尾内容');
    const expand = wrapper.findAll('button').find(b => b.text().includes('展开全文'));
    expect(expand).toBeTruthy();
    // The control names the CHARACTER count (symbols and spaces included), not
    // the line count: "20 lines" bounds nothing when the lines are numbers.
    expect(expand!.text()).toContain(String(long.length));
    expect(expand!.text()).toContain('字');

    // Expanding renders the full text; collapsing restores the preview.
    await expand!.trigger('click');
    expect(wrapper.text()).toContain('tail-0');
    const collapse = wrapper.findAll('button').find(b => b.text().includes('收起全文'));
    expect(collapse).toBeTruthy();
    await collapse!.trigger('click');
    expect(wrapper.text()).not.toContain('tail-0');
  });

  it('collapses a settled answer past 2500 CHARACTERS, and leaves 2500 alone', () => {
    // The trigger counts characters (symbols and spaces included): the boundary
    // is pinned here so a change to the cap is a deliberate edit.
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 70, role: CHAT_ROLE.AI, content: 'x'.repeat(2500) })] }
    });
    expect(wrapper.findAll('button').some(b => b.text().includes('展开全文'))).toBe(false);

    const long = base({ id: 71, role: CHAT_ROLE.AI, content: 'x'.repeat(2501) });
    const longWrapper = mount(ChatBox, { props: { messages: [long] } });
    const expand = longWrapper.findAll('button').find(b => b.text().includes('展开全文'));
    expect(expand).toBeTruthy();
    expect(expand!.text()).toContain('2501');
  });

  it('leaves a short streaming answer untouched', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 61, role: CHAT_ROLE.AI, content: 'short answer', streaming: true })]
      }
    });
    expect(wrapper.text()).toContain('short answer');
    expect(wrapper.text()).not.toContain('仅显示末尾内容');
  });

  it('opens the session context viewer when a token figure is clicked', async () => {
    // The token phrase on the AI row is the affordance: hovering it turns the
    // numbers blue (the class) and clicking opens the 当前会话 context tab.
    const sidebar = { openTab: vi.fn(() => 'contextViewer-1') };
    vi.stubGlobal('useRightSidebarStore', () => sidebar);
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 41,
            role: CHAT_ROLE.AI,
            content: '答复',
            modelName: 'glm-4.6',
            inputTokens: 13186,
            outputTokens: 66
          })
        ]
      }
    });

    const trigger = wrapper.get('[data-test="model-meta-open-context"]');
    expect(trigger.text()).toContain('13186');
    expect(trigger.classes().join(' ')).toContain('hover:text-[#2563EB]');
    expect(trigger.attributes('title')).toBeTruthy();

    await trigger.trigger('click');

    expect(sidebar.openTab).toHaveBeenCalledWith('contextViewer');
    // Every figure in every bubble lands in that one tab (no payload → the
    // store's own dedupe keeps a single instance).
    await trigger.trigger('click');
    expect(sidebar.openTab).toHaveBeenCalledTimes(2);
    expect(sidebar.openTab).toHaveReturnedWith('contextViewer-1');
  });
});
