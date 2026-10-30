/**
 * The chat's find bar: a controlled input over `useConversationFind`. It shows
 * the match counter, forwards the query / step / close intents, disables the
 * arrows without matches and reddens the box for a miss.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

const i18n = vi.hoisted(() => ({ t: (key: string) => key }));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import ConversationFindBar from '../ConversationFindBar.vue';

function mountBar(props: Record<string, unknown> = {}) {
  return mount(ConversationFindBar, {
    props: { visible: true, total: 3, currentIndex: 0, ...props }
  });
}

describe('ConversationFindBar', () => {
  it('renders nothing while closed', () => {
    const wrapper = mountBar({ visible: false });

    expect(wrapper.find('[data-test="conversation-find-bar"]').exists()).toBe(false);
  });

  it('shows the 1-based counter', () => {
    const wrapper = mountBar({ total: 4, currentIndex: 2 });

    return wrapper
      .find('[data-test="conversation-find-input"]')
      .setValue('q')
      .then(() => {
        expect(wrapper.find('[data-test="conversation-find-count"]').text()).toBe('3/4');
      });
  });

  it('reads 0/0 before anything is typed', () => {
    const wrapper = mountBar({ total: 0, currentIndex: -1 });

    expect(wrapper.find('[data-test="conversation-find-count"]').text()).toBe('0/0');
  });

  it('marks a miss red and disables the step buttons', async () => {
    const wrapper = mountBar({ total: 0, currentIndex: -1 });
    const input = wrapper.find('[data-test="conversation-find-input"]');

    await input.setValue('nope');

    expect((input.element as HTMLInputElement).classList.contains('text-red-500')).toBe(true);
    expect(wrapper.find('[data-test="conversation-find-count"]').text()).toBe('0/0');
    expect(wrapper.find('[data-test="conversation-find-next"]').attributes('disabled')).toBeDefined();
    expect(wrapper.find('[data-test="conversation-find-previous"]').attributes('disabled')).toBeDefined();
  });

  it('forwards the typed query', async () => {
    const wrapper = mountBar();

    await wrapper.find('[data-test="conversation-find-input"]').setValue('needle');

    expect(wrapper.emitted('search')).toEqual([['needle']]);
  });

  it('Enter steps forward, Shift+Enter backward, Esc closes', async () => {
    const wrapper = mountBar();
    const input = wrapper.find('[data-test="conversation-find-input"]');

    await input.trigger('keydown', { key: 'Enter' });
    await input.trigger('keydown', { key: 'Enter', shiftKey: true });
    await input.trigger('keydown', { key: 'Escape' });

    expect(wrapper.emitted('next')).toHaveLength(1);
    expect(wrapper.emitted('previous')).toHaveLength(1);
    expect(wrapper.emitted('close')).toHaveLength(1);
  });

  it('forwards the arrow and close buttons', async () => {
    const wrapper = mountBar();

    await wrapper.find('[data-test="conversation-find-next"]').trigger('click');
    await wrapper.find('[data-test="conversation-find-previous"]').trigger('click');
    await wrapper.find('[data-test="conversation-find-close"]').trigger('click');

    expect(wrapper.emitted('next')).toHaveLength(1);
    expect(wrapper.emitted('previous')).toHaveLength(1);
    expect(wrapper.emitted('close')).toHaveLength(1);
  });

  it('drops the draft when the bar closes', async () => {
    const wrapper = mountBar();
    const input = wrapper.find('[data-test="conversation-find-input"]');
    await input.setValue('needle');

    await wrapper.setProps({ visible: false });
    await wrapper.setProps({ visible: true });

    expect((wrapper.find('[data-test="conversation-find-input"]').element as HTMLInputElement).value).toBe('');
  });
});
