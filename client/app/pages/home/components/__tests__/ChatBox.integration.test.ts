import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatBox from '@/pages/home/components/ChatBox.vue';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';

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
  it('renders a single AI message with name "橘雪莉" and sanitized markdown', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.AI, content: '**bold** world' })]
      }
    });
    expect(wrapper.text()).toContain('橘雪莉');
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
    // 2 avatars exist (one per message)
    const avatars = wrapper.findAll('.pi-user');
    expect(avatars).toHaveLength(2);
    // first avatar visible, second hidden (previous message shares the role)
    expect(avatars[0].classes()).not.toContain('hidden');
    expect(avatars[1].classes()).toContain('hidden');
  });

  it('shows empty state when no messages', () => {
    const wrapper = mount(ChatBox, { props: { messages: [] } });
    expect(wrapper.find('.flex-1').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('橘雪莉');
  });
});

// ── Background-task system card (subagent-origin-tagging Task 5) ─────────────
// Backend history rows whose origin column is "subagent_completion" are background-task
// completion carriers (USER-role rows). They must render as a centered, muted system
// card — never as a user bubble. The carrier's first line "[subagent:<name> <status>]"
// is self-describing and shown verbatim (no parsing).

/** Realistic carrier content (matches agent/tools/subagent/announce/completion_message.py format) */
const CARRIER = '[subagent:研究员 done]\n后台检索已完成，结果已送达主会话。';

describe('ChatBox background-task system card (integration, backend mocked)', () => {
  it('renders a USER message with origin as a centered muted system card, not a user bubble', () => {
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
    // Carrier text shown verbatim: the [subagent:...] first line is NOT parsed away
    expect(wrapper.text()).toContain('[subagent:研究员 done]');
    expect(wrapper.text()).toContain('后台检索已完成，结果已送达主会话。');
    // User-bubble markup absent: no blue bubble, no right-reversed row flow
    expect(wrapper.html()).not.toContain('bg-[#2563EB]');
    expect(wrapper.html()).not.toContain('flex-row-reverse');
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
