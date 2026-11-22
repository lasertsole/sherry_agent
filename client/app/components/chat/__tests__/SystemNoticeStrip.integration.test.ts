/**
 * The transient system.notice strip: frames arrive on the process-global mitt
 * bus (`ws:system_notice`, dispatched from the agent socket), each card is
 * bound to its session, dismissal is per card, and a real unmount drops the
 * session's cards (KeepAlive deactivation keeps them).
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';

const i18n = vi.hoisted(() => ({ t: (key: string) => key }));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import SystemNoticeStrip from '../SystemNoticeStrip.vue';
import { emit } from '@/composables/mitt';
import { clearSessionNotices, useSystemNotices } from '~/composables/use-system-notice';

const fireNotice = (sessionId: string, title: string) =>
  emit('ws:system_notice', {
    event: 'system_notice',
    session_id: sessionId,
    content: { title, content: '$ sleep 5', level: 'success', source: 'tool:terminal' }
  });

describe('SystemNoticeStrip.vue', () => {
  /** Live wrappers of this suite: unmounted in afterEach (each holds a mitt subscription). */
  const wrappers: VueWrapper[] = [];

  const mountStrip = (sessionId: string) => {
    const wrapper = mount(SystemNoticeStrip, { props: { sessionId } });
    wrappers.push(wrapper);
    return wrapper;
  };

  beforeEach(() => {
    clearSessionNotices('s1');
    clearSessionNotices('s2');
    useSystemNotices().notices.value = [];
  });

  afterEach(() => {
    while (wrappers.length) wrappers.pop()?.unmount();
  });

  it('renders nothing without notices', () => {
    const wrapper = mountStrip('s1');
    expect(wrapper.find('[data-test="system-notice-strip"]').exists()).toBe(false);
  });

  it('a frame for its session renders a card with title, source and content', async () => {
    const wrapper = mountStrip('s1');
    fireNotice('s1', 'Background sleep exited (code 0)');
    await wrapper.vm.$nextTick();

    const strip = wrapper.find('[data-test="system-notice-strip"]');
    expect(strip.exists()).toBe(true);
    expect(strip.text()).toContain('Background sleep exited (code 0)');
    expect(strip.text()).toContain('tool:terminal');
    expect(strip.text()).toContain('$ sleep 5');
    expect(strip.find('[data-notice-level="success"]').exists()).toBe(true);
  });

  it('a frame for ANOTHER session never renders here', async () => {
    const wrapper = mountStrip('s1');
    fireNotice('s2', 'other session');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="system-notice-strip"]').exists()).toBe(false);
  });

  it('the dismiss button removes the card', async () => {
    const wrapper = mountStrip('s1');
    fireNotice('s1', 'transient');
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="system-notice-dismiss"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="system-notice-strip"]').exists()).toBe(false);
    expect(useSystemNotices().notices.value).toHaveLength(0);
  });

  it('a real unmount drops the session cards (the strip only ever recorded its own)', async () => {
    // Seed another session's card through the composable directly: the strip
    // never records foreign frames, so unmount must not touch it.
    useSystemNotices().push({
      session_id: 's2',
      content: { title: 'theirs', content: '', level: 'info', source: 'external' }
    });
    const wrapper = mountStrip('s1');
    fireNotice('s1', 'mine');

    wrapper.unmount();
    const remaining = useSystemNotices().notices.value;
    expect(remaining.map(n => n.sessionId)).toEqual(['s2']);
    expect(remaining[0]!.title).toBe('theirs');
  });
});
