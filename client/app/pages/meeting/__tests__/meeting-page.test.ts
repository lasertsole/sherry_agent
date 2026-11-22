/**
 * S4 meeting page: `/meeting/:id`. Pins the header (topic / status / iteration),
 * the participant plan column, the message stream built from MessageBubble, the
 * interrupt box, and the execute / confirm-end hand-offs. The store owns the
 * mock backend (see stores/__tests__/meeting.test.ts).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createTestingPinia } from '@pinia/testing';
import { setActivePinia } from 'pinia';

const MEETING_ID = 'meeting-demo';

// `definePageMeta` is a Nuxt compile-time macro; the Vitest build keeps it as a
// bare identifier, so it must exist before the page module is evaluated.
vi.hoisted(() => {
  (globalThis as unknown as Record<string, unknown>).definePageMeta = () => {};
});

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: MEETING_ID }, path: `/meeting/${MEETING_ID}`, query: {} }),
  useRouter: () => ({ push: vi.fn() })
}));

import MeetingPage from '../[id].vue';
import { useMeetingStore } from '@/stores/meeting';

const push = vi.fn();

/** PrimeVue is not installed in the test bootstrap; stub the widgets used. */
const globalStubs = {
  Button: {
    name: 'Button',
    props: ['label', 'icon', 'severity', 'size', 'text', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}<slot /></button>'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue', 'keyup'],
    template:
      '<input :value="modelValue" :placeholder="placeholder" ' +
      '@input="$emit(\'update:modelValue\', $event.target.value)" ' +
      '@keyup.enter="$emit(\'keyup\', $event)" />'
  }
};

async function mountPage() {
  const wrapper = mount(MeetingPage, { global: { stubs: globalStubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createTestingPinia({ stubActions: false, createSpy: vi.fn }));
  push.mockReset();
  vi.stubGlobal('useRouter', () => ({ push }));
});

describe('meeting page (S4)', () => {
  it('opens the meeting from the route id and renders its header', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();

    expect(store.currentMeeting?.id).toBe(MEETING_ID);
    expect(wrapper.get('[data-test="meeting-page"]').text()).toContain('架构评审');
    expect(wrapper.text()).toContain('4/40');
  });

  it('renders the execution plan phases as the participant column', async () => {
    const wrapper = await mountPage();

    const participants = wrapper.get('[data-test="meeting-participants"]');
    expect(participants.text()).toContain('discussion');
    expect(participants.text()).toContain('conclusion');
  });

  it('renders one bubble per message, with speaker labels', async () => {
    const wrapper = await mountPage();

    const bubbles = wrapper.findAll('[data-test="meeting-message"]');
    expect(bubbles.length).toBe(2);
    expect(bubbles[0]!.text()).toContain('主持人');
    expect(bubbles[1]!.text()).toContain('架构专家');
  });

  it('shows the empty-stream placeholder when no message arrived yet', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();

    store.messages = [];
    await flushPromises();

    expect(wrapper.find('[data-test="meeting-message"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="meeting-empty-stream"]').exists()).toBe(true);
  });

  it('sends an interrupt and clears the input', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();
    const before = store.messages.length;

    const input = wrapper.get('[data-test="meeting-interrupt-input"]');
    await input.setValue('补充一个约束');
    await wrapper.get('[data-test="meeting-interrupt-send"]').trigger('click');
    await flushPromises();

    expect(store.messages.length).toBe(before + 1);
    expect(store.messages.at(-1)?.content).toBe('补充一个约束');
    expect((input.element as HTMLInputElement).value).toBe('');
  });

  it('ignores an empty interrupt', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();
    const before = store.messages.length;

    await wrapper.get('[data-test="meeting-interrupt-send"]').trigger('click');
    await flushPromises();

    expect(store.messages.length).toBe(before);
  });

  it('executes the plan and returns to the chat shell', async () => {
    const wrapper = await mountPage();

    await wrapper.get('[data-test="meeting-execute"]').trigger('click');
    await flushPromises();

    expect(push).toHaveBeenCalledWith('/home');
  });

  it('ends the meeting on confirm', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();

    await wrapper.get('[data-test="meeting-confirm-end"]').trigger('click');
    await flushPromises();

    expect(store.currentMeeting?.status).toBe('ended');
  });

  it('releases the store state when the page unmounts', async () => {
    const wrapper = await mountPage();
    const store = useMeetingStore();

    wrapper.unmount();

    expect(store.currentMeeting).toBeNull();
    expect(store.messages).toEqual([]);
  });
});
