/**
 * CronPanel skill binding: the form's skill picker and the card's chips.
 *
 * The picker is the UI half of the cron→skill binding — it must load the
 * installed skills, pre-check a job's bound names when editing, and send the
 * selection (or an explicit null when cleared) through addCronJob/updateCronJob.
 * A picker that silently dropped the selection would leave the job running
 * without the skill instructions the user asked for.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import CronPanel from '@/pages/home/components/CronPanel.vue';

const bridge = vi.hoisted(() => ({
  listCronJobs: vi.fn(),
  addCronJob: vi.fn(),
  updateCronJob: vi.fn(),
  deleteCronJob: vi.fn(),
  enableCronJob: vi.fn(),
  runCronJob: vi.fn(),
  listSkills: vi.fn()
}));

vi.mock('~/composables/bridge', () => bridge);

const SKILLS = [
  { name: 'news-digest', description: 'd', location: 'auto/news-digest', category: 'auto' },
  { name: 'github-auth', description: 'd', location: 'auto/github-auth', category: 'auto' }
];

const JOB = {
  id: 'j1',
  name: 'morning',
  enabled: true,
  schedule: { kind: 'every', everyMs: 300000 },
  payload: { kind: 'agent_turn', message: 'say hi', deliver: false, skills: ['news-digest'] },
  state: {},
  deleteAfterRun: false
};

const stubs = {
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header', 'modal', 'closable'],
    emits: ['update:visible'],
    template: '<div class="dlg"><slot /><slot name="footer" /></div>'
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea class="ta" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  SelectButton: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<div class="sb" />' },
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<div class="sel" />' },
  InputNumber: { props: ['modelValue'], emits: ['update:modelValue'], template: '<div class="in" />' },
  Calendar: { props: ['modelValue'], emits: ['update:modelValue'], template: '<div class="cal" />' },
  ToggleSwitch: { props: ['modelValue', 'disabled'], template: '<span class="ts" />' },
  ProgressSpinner: { template: '<div class="spinner" />' },
  // Emulates PrimeVue's two modes: `binary` (a bool) and multi-select (an
  // array + `:value`), so the test drives it with real clicks.
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue', 'value', 'binary', 'inputId'],
    emits: ['update:modelValue'],
    computed: {
      checked(): boolean {
        const mv = this.modelValue as unknown;
        return Array.isArray(mv) ? mv.includes(this.value as never) : !!mv;
      }
    },
    methods: {
      toggle(): void {
        if (this.binary) {
          this.$emit('update:modelValue', !this.modelValue);
          return;
        }
        const list = Array.isArray(this.modelValue) ? [...(this.modelValue as string[])] : [];
        const i = list.indexOf(this.value as string);
        if (i >= 0) list.splice(i, 1);
        else list.push(this.value as string);
        this.$emit('update:modelValue', list);
      }
    },
    template: '<span class="cb" :data-value="value" :data-checked="checked" @click="toggle" />'
  }
};

/** Mount with both loads answered (jobs + skills). */
async function mountPanel() {
  const wrapper = mount(CronPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

/**
 * The skill checkboxes rendered inside the (always-mounted) dialog stub.
 * @param wrapper
 */
const skillBoxes = (wrapper: ReturnType<typeof mount>) => wrapper.findAll('.dlg .cb[data-value]');

describe('CronPanel skill binding', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.listCronJobs.mockResolvedValue({ jobs: [] });
    bridge.listSkills.mockResolvedValue({ skills: SKILLS });
    bridge.addCronJob.mockResolvedValue({ success: true });
    bridge.updateCronJob.mockResolvedValue({ success: true });
  });

  it('lists the installed skills as unselected checkboxes by default', async () => {
    const wrapper = await mountPanel();

    const boxes = skillBoxes(wrapper);
    expect(boxes.map(b => b.attributes('data-value'))).toEqual(['news-digest', 'github-auth']);
    expect(boxes.every(b => b.attributes('data-checked') === 'false')).toBe(true);
    expect(wrapper.text()).toContain('定时任务决定何时执行');
  });

  it('shows the empty hint when no skills are installed', async () => {
    bridge.listSkills.mockResolvedValue({ skills: [] });
    const wrapper = await mountPanel();

    expect(skillBoxes(wrapper)).toHaveLength(0);
    expect(wrapper.text()).toContain('暂无可用技能');
  });

  it('shows bound skills as chips on the job card', async () => {
    bridge.listCronJobs.mockResolvedValue({ jobs: [JOB] });
    const wrapper = await mountPanel();

    const card = wrapper.find('.flex.flex-col.gap-2 > div');
    expect(card.text()).toContain('news-digest');
  });

  it('pre-checks the job\u2019s skills when editing', async () => {
    bridge.listCronJobs.mockResolvedValue({ jobs: [JOB] });
    const wrapper = await mountPanel();

    // Click the card's edit button (the second action button).
    const editButtons = wrapper.findAll('button.btn').filter(b => b.text() === '编辑');
    await editButtons[0]!.trigger('click');
    await flushPromises();

    const boxes = skillBoxes(wrapper);
    expect(boxes.map(b => b.attributes('data-checked'))).toEqual(['true', 'false']);
  });

  it('sends the selected skills on save and null when cleared', async () => {
    bridge.listCronJobs.mockResolvedValue({ jobs: [JOB] });
    const wrapper = await mountPanel();

    const editButtons = wrapper.findAll('button.btn').filter(b => b.text() === '编辑');
    await editButtons[0]!.trigger('click');
    await flushPromises();

    // Add the second skill to the pre-checked first one, then save.
    await skillBoxes(wrapper)[1]!.trigger('click');
    await flushPromises();
    expect(skillBoxes(wrapper).map(b => b.attributes('data-checked'))).toEqual(['true', 'true']);

    const save = wrapper.findAll('button.btn').find(b => b.text() === '保存');
    await save!.trigger('click');
    await flushPromises();

    expect(bridge.updateCronJob).toHaveBeenCalledTimes(1);
    const [, patch] = bridge.updateCronJob.mock.calls[0]!;
    expect(patch.skills).toEqual(['news-digest', 'github-auth']);
  });

  it('clearing every skill sends null (the job runs without a binding)', async () => {
    bridge.listCronJobs.mockResolvedValue({ jobs: [JOB] });
    const wrapper = await mountPanel();

    const editButtons = wrapper.findAll('button.btn').filter(b => b.text() === '编辑');
    await editButtons[0]!.trigger('click');
    await flushPromises();

    await skillBoxes(wrapper)[0]!.trigger('click'); // uncheck the bound skill
    await flushPromises();
    expect(skillBoxes(wrapper).every(b => b.attributes('data-checked') === 'false')).toBe(true);

    const save = wrapper.findAll('button.btn').find(b => b.text() === '保存');
    await save!.trigger('click');
    await flushPromises();

    const [, patch] = bridge.updateCronJob.mock.calls[0]!;
    expect(patch.skills).toBeNull();
  });
});
