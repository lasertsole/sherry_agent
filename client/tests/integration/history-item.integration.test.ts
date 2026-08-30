import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import type { SessionRecord } from '@/pages/home/type';

const record: SessionRecord = {
  id: 's1',
  title: '第一次对话',
  // The component formats createTime via formatCompactTimeString, which strictly
  // requires the backend's 14-digit compact string (YYYYMMDDHHmmss).
  createTime: '20260617104200'
};

// PrimeVue Menu is explicitly imported and renders via Teleport, which is
// unreliable under happy-dom; a stub also lets us drive openHeaderMenu safely.
const mountItem = (props: Record<string, unknown>) =>
  mount(HistoryItem, {
    props,
    global: {
      stubs: {
        Menu: {
          template: '<div class="mnu"><slot /></div>',
          methods: { toggle() {} }
        },
        Checkbox: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template:
            '<button class="cb" @click="$emit(\'update:modelValue\', [...(modelValue || []), \'s1\'])">CB</button>'
        },
        // Rendered when the row enters inline rename mode (startEdit).
        InputText: { template: '<input class="it" />' }
      }
    }
  });

describe('HistoryItem.vue (integration, backend mocked)', () => {
  it('renders the session title and create time', () => {
    const wrapper = mountItem({ historyRecord: record, isActive: false });
    expect(wrapper.text()).toContain('第一次对话');
    // createTime renders through t('history.createdAt') after formatCompactTimeString:
    // the zh template ("created at: {time}") + dateFormat 'YYYY-MM-DD HH:mm' -> the zh prefix plus 2026-06-17 10:42
    expect(wrapper.text()).toContain('创建时间：');
    expect(wrapper.text()).toContain('2026-06-17 10:42');
  });

  it('emits chooseSession with the record id on click', async () => {
    const wrapper = mountItem({ historyRecord: record, isActive: false });
    // The delete/rename icon (.pi-ellipsis-h) calls @click.stop, so clicking the
    // row body (not the icon) must emit chooseSession.
    await wrapper.find('.p-3').trigger('click');
    const emitted = wrapper.emitted('chooseSession');
    expect(emitted).toBeTruthy();
    expect((emitted as unknown as [string][])[0][0]).toBe('s1');
  });

  it('uses isActive-prop styling for the active session', () => {
    const active = mountItem({ historyRecord: record, isActive: true });
    // Active rows highlight via bg-[#c1d6e5]! (text-theme-main is already in the base class list).
    expect(active.classes()).toContain('bg-[#c1d6e5]!');

    const inactive = mountItem({ historyRecord: record, isActive: false });
    expect(inactive.classes()).not.toContain('bg-[#c1d6e5]!');
  });

  it('forwards defineModel selectedList updates back to the parent', async () => {
    // The Checkbox stub re-emits the whole selected list (plus a sentinel) on
    // click; the component must bubble it up through defineModel.
    const wrapper = mountItem({
      historyRecord: record,
      isActive: false,
      selectedList: ['s0', 's1']
    });
    await wrapper.find('button.cb').trigger('click');
    const updates = wrapper.emitted('update:selectedList') as unknown as string[][][];
    expect(updates).toBeTruthy();
    expect(updates[0][0]).toContain('s1');
  });

  it('never emits chooseSession when the row action icon is clicked', async () => {
    const wrapper = mountItem({ historyRecord: record, isActive: false });
    // .pi-pencil (rename entry) has @click.stop -> enters inline edit, not chooseSession.
    await wrapper.find('.pi-pencil').trigger('click');
    expect(wrapper.emitted('chooseSession')).toBeFalsy();
  });
});
