import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import MediaMenu from '@/pages/home/components/MediaMenu.vue';

// The Menu popup stub mirrors PrimeVue's popup surface: the option list only
// exists while "opened", and toggle() flips it (same stub as the pickers').
const MenuStub = {
  name: 'Menu',
  props: ['model', 'popup'],
  data: () => ({ opened: false }),
  methods: {
    toggle() {
      this.opened = !this.opened;
    }
  },
  template: `<div v-if="opened" class="menu-stub"><button v-for="i in model" :key="i.label" class="opt" @click="i.command()"><i v-if="i.icon" :class="i.icon"></i>{{ i.label }}</button></div>`
};

const stubs = {
  Button: {
    props: ['label', 'variant', 'size', 'icon', 'iconPos', 'ariaLabel'],
    emits: ['click'],
    template:
      '<button class="trigger" :aria-label="ariaLabel" @click="$emit(\'click\', $event)">{{ label }}<i v-if="icon" :class="icon"></i></button>'
  },
  Menu: MenuStub
};

function mountMenu() {
  return mount(MediaMenu, { global: { stubs } });
}

describe('MediaMenu.vue (integration)', () => {
  it('collapses image / audio / video into one 多媒体 entry', () => {
    const wrapper = mountMenu();

    // One trigger naming the category; the three kinds are not in the row.
    expect(wrapper.find('button.trigger').text()).toContain('多媒体');
    expect(wrapper.text()).not.toContain('图片');
    expect(wrapper.findAll('button.opt').length).toBe(0);
  });

  it('lists the three media kinds with their icons when opened', async () => {
    const wrapper = mountMenu();

    await wrapper.find('button.trigger').trigger('click');

    const options = wrapper.findAll('button.opt');
    expect(options.map(o => o.text())).toEqual(['图片', '音频', '视频']);
    expect(options.map(o => o.find('i').classes()[1])).toEqual(['pi-image', 'pi-microphone', 'pi-video']);
  });

  it('emits the toolbar event of the picked kind', async () => {
    const wrapper = mountMenu();

    await wrapper.find('button.trigger').trigger('click');
    await wrapper.findAll('button.opt')[1]!.trigger('click');
    await wrapper.findAll('button.opt')[2]!.trigger('click');

    // One emission per pick, in pick order; the page dispatches them through
    // the session toolbar command registry.
    expect(wrapper.emitted('select')).toEqual([['uploadAudio'], ['uploadVideo']]);
  });

  it('names the control for screen readers', () => {
    const wrapper = mountMenu();
    expect(wrapper.find('button.trigger').attributes('aria-label')).toBe('选择要上传的多媒体类型');
  });

  it('toggles the popup on every trigger click', async () => {
    const wrapper = mountMenu();

    await wrapper.find('button.trigger').trigger('click');
    expect(wrapper.find('.menu-stub').exists()).toBe(true);
    await wrapper.find('button.trigger').trigger('click');
    expect(wrapper.find('.menu-stub').exists()).toBe(false);
  });
});
