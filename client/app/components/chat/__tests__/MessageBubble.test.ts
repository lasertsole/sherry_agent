/**
 * MessageBubble is the shared render template for chat and meeting messages.
 * Pin speaker/role/thinking/actions so S4 can reuse it without re-implementing
 * markdown rendering or copy/fork affordances.
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import MessageBubble from '../MessageBubble.vue';

describe('MessageBubble', () => {
  it('renders content without speaker chrome by default', () => {
    const wrapper = mount(MessageBubble, { props: { content: '<p>hello</p>' } });
    expect(wrapper.get('[data-test="message-bubble-content"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="message-bubble-speaker"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="message-bubble-thinking"]').exists()).toBe(false);
  });

  it('shows speaker name and role badge for meeting messages', () => {
    const wrapper = mount(MessageBubble, {
      props: { content: 'x', speakerName: 'Alice', roleBadge: '总结' }
    });
    expect(wrapper.get('[data-test="message-bubble-speaker"]').text()).toContain('Alice');
    expect(wrapper.get('[data-test="message-bubble-role"]').text()).toBe('总结');
  });

  it('renders collapsible thinking with the thinking label', () => {
    const wrapper = mount(MessageBubble, {
      props: { content: 'x', thinking: '<p>hmm</p>' }
    });
    expect(wrapper.get('[data-test="message-bubble-thinking"] summary').text()).toBe('思考过程');
  });

  it('forwards the actions slot', () => {
    const wrapper = mount(MessageBubble, {
      props: { content: 'x' },
      slots: { actions: '<button data-test="act">copy</button>' }
    });
    expect(wrapper.find('[data-test="act"]').exists()).toBe(true);
  });
});
