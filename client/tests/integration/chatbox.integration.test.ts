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
  ...over,
});

describe('ChatBox.vue (integration, backend mocked)', () => {
  it('renders a single AI message with name "橘雪莉" and sanitized markdown', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.AI, content: '**bold** world' })],
      },
    });
    expect(wrapper.text()).toContain('橘雪莉');
    expect(wrapper.text()).toContain('world');
    // markdown-it turns **bold** into <strong> inside the sanitized v-html
    expect(wrapper.find('.w-fit').html()).toContain('<strong>bold</strong>');
  });

  it('credits human messages as 我', () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'hi' })] },
    });
    expect(wrapper.text()).toContain('我');
  });

  it('filters out TOOL role messages entirely', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.TOOL, content: 'tool-call' }),
          base({ id: 2, role: CHAT_ROLE.USER, content: '被保留' }),
        ],
      },
    });
    const text = wrapper.text();
    expect(text).not.toContain('tool-call');
    expect(text).toContain('被保留');
  });

  it('renders the tool_calls indicator block', () => {
    // `tool_calls` is accessed with optional chaining in the component, so it is
    // safe to pass even though `MessageItem` does not declare it.
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          {
            ...base({ id: 1, role: CHAT_ROLE.AI, content: 'doing something' }),
            tool_calls: [{ id: 't1', name: 'web_search', arguments: '' }],
          } as any,
        ],
      },
    });
    expect(wrapper.text()).toContain('正在调用工具web_search');
  });

  it('strips <script> tags from user content via markdown-it + DOMPurify', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({ id: 1, role: CHAT_ROLE.USER, content: '<script>alert(1)</script>hi' }),
        ],
      },
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
          base({ id: 2, role: CHAT_ROLE.USER, content: 'second' }),
        ],
      },
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
