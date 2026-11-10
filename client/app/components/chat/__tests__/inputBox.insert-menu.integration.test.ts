/**
 * The composer's + menu: one entry point for attachments and the `$` / `/`
 * pickers.
 *
 * The menu absorbed the toolbar's former MediaMenu (image / audio / video) and
 * the file entry now opens the OS picker instead of revealing the sidebar tree,
 * so this suite pins the emitted events the session page binds to its hidden
 * inputs, plus the two picker entries that open the in-composer panel.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import inputBox from '@/components/chat/inputBox.vue';

vi.mock('~/composables/bridge/voice', () => ({
  fetchVoiceStatus: vi.fn(async () => ({ model_dir_present: false, daemon_ready: false })),
  requestTranscription: vi.fn(async () => '')
}));

const ButtonStub = {
  props: ['label', 'disabled'],
  emits: ['click'],
  template: '<button @click="$emit(\'click\', $event)"><slot /></button>'
};

/** Calls the plus button made into the (stubbed) Popover's toggle. */
let toggleCalls: Array<{ event: Event; target?: HTMLElement }> = [];

/** PrimeVue Popover's public surface: toggle/hide + the slotted content. */
const PopoverStub = {
  name: 'Popover',
  setup(_props: unknown, { slots, expose }: any) {
    expose({
      toggle: (event: Event, target?: HTMLElement) => toggleCalls.push({ event, target }),
      hide: () => {}
    });
    return () => slots.default?.();
  }
};

const TriggerPanelStub = {
  name: 'InputTriggerPanel',
  props: ['kind', 'query', 'anchor', 'sessionId'],
  template: '<div data-test="trigger-panel-stub" :data-kind="kind" />'
};

function mountComposer(props: Record<string, unknown> = {}) {
  return mount(inputBox, {
    props,
    global: {
      stubs: {
        Button: ButtonStub,
        Popover: PopoverStub,
        InputTriggerPanel: TriggerPanelStub
      }
    }
  });
}

/**
 * The menu entries in template order.
 * @param wrapper The mounted composer.
 * @returns The six `.insert-item` buttons.
 */
const items = (wrapper: ReturnType<typeof mountComposer>) => wrapper.findAll('.insert-item');

describe('inputBox.vue + menu (integration, pickers stubbed)', () => {
  it('anchors the popup to the + trigger even though the click is debounced', async () => {
    toggleCalls = [];
    const wrapper = mountComposer();
    const trigger = wrapper.get('.insert-trigger');

    await trigger.trigger('click');
    // The debounce holds the toggle back…
    expect(toggleCalls).toHaveLength(0);
    await new Promise(resolve => setTimeout(resolve, 350));

    // …and when it runs, `currentTarget` is gone, so the target is passed explicitly.
    expect(toggleCalls).toHaveLength(1);
    expect(toggleCalls[0]!.target).toBe(trigger.element);
  });

  it('offers the three uploads, the file entry, a separator and the two pickers', () => {
    const wrapper = mountComposer();
    const labels = items(wrapper).map(item => item.text());

    expect(labels).toHaveLength(6);
    expect(labels[0]).toContain('图片上传');
    expect(labels[1]).toContain('音频上传');
    expect(labels[2]).toContain('视频上传');
    expect(labels[3]).toContain('添加文件');
    expect(labels[4]).toContain('技能');
    expect(labels[5]).toContain('命令');

    // The file entry carries the paperclip (not the old folder/@ shortcut).
    const file = items(wrapper)[3]!;
    expect(file.find('.pi-paperclip').exists()).toBe(true);
    expect(file.find('code').exists()).toBe(false);
    expect(wrapper.find('.insert-menu-divider').attributes('role')).toBe('separator');
  });

  it('hides the media uploads on a surface without an attachment tray (side chat)', () => {
    const wrapper = mountComposer({ mediaUploads: false });
    const labels = items(wrapper).map(item => item.text());

    expect(labels).toHaveLength(3);
    expect(labels[0]).toContain('添加文件');
    expect(labels[1]).toContain('技能');
    expect(labels[2]).toContain('命令');
    expect(wrapper.find('.insert-menu-divider').exists()).toBe(false);
  });

  it('emits the upload events the page binds to its hidden inputs', async () => {
    const wrapper = mountComposer();
    const menuItems = items(wrapper);

    await menuItems[0]!.trigger('click');
    await menuItems[1]!.trigger('click');
    await menuItems[2]!.trigger('click');
    await menuItems[3]!.trigger('click');

    expect(wrapper.emitted('uploadImage')).toHaveLength(1);
    expect(wrapper.emitted('uploadAudio')).toHaveLength(1);
    expect(wrapper.emitted('uploadVideo')).toHaveLength(1);
    expect(wrapper.emitted('uploadFile')).toHaveLength(1);
  });

  it('opens the skill / command picker panel after the menu hides', async () => {
    const wrapper = mountComposer();
    const menuItems = items(wrapper);

    await menuItems[4]!.trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="trigger-panel-stub"]').attributes('data-kind')).toBe('skill');

    await menuItems[5]!.trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="trigger-panel-stub"]').attributes('data-kind')).toBe('slash');
  });
});
