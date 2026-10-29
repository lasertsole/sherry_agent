/**
 * The Ctrl+K command palette (integration).
 *
 * Contract: rows come from the registry (never a hardcoded table), the label
 * highlight is escaped HTML, the keyboard cursor walks the flat result list and
 * Enter runs the highlighted command, Escape closes, an unmatched query shows
 * the empty state, and the recent-history block runs a still-registered command.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import CommandPalette from '@/components/CommandPalette.vue';
import { registerStaticBatch, resetCommandRegistry, type CommandEntry } from '@/composables/use-command-registry';

const history = vi.hoisted(() => ({
  recordCommandHistory: vi.fn(async () => {}),
  recentCommandHistory: vi.fn(async () => [] as Array<Record<string, unknown>>)
}));
vi.mock('~/composables/use-command-history', () => history);

const skillsBridge = vi.hoisted(() => ({ listSkills: vi.fn(async () => ({ skills: [] })) }));
vi.mock('~/composables/bridge/skills', () => skillsBridge);

const messages = vi.hoisted(() => ({ getSessionList: vi.fn(async () => []) }));
vi.mock('~/composables/messages', () => messages);

/**
 * A passthrough Dialog: the palette's own logic (not PrimeVue's visibility) is
 * under test, so the slot always renders and `show`/`hide` are emitted by hand.
 */
const DialogStub = {
  name: 'Dialog',
  props: ['visible'],
  emits: ['update:visible', 'show', 'hide'],
  template: '<div data-test="dialog-stub"><slot /></div>'
};

/** A real <input> so `v-model` and keydown behave like production. */
const InputTextStub = {
  props: ['modelValue', 'placeholder'],
  emits: ['update:modelValue'],
  // `@keydown` is NOT declared as an emit: an undeclared listener falls through
  // to this root <input> as a native handler, so the palette receives the real
  // KeyboardEvent (declaring it would need a manual re-emit and could double-fire).
  template:
    '<input class="palette-input" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
};

/**
 * A command with an inert handler.
 * @param overrides Fields to override.
 */
function command(overrides: Partial<CommandEntry> = {}): CommandEntry {
  return {
    id: 'cmd',
    label: 'Command',
    section: 'tools',
    icon: 'pi pi-bolt',
    handler: vi.fn(),
    ...overrides
  };
}

/**
 * Mount the palette with the PrimeVue stubs above.
 * @param props Extra props (e.g. the visible model).
 */
function mountPalette(props: Record<string, unknown> = {}) {
  return mount(CommandPalette, {
    props: { visible: true, ...props },
    global: { stubs: { Dialog: DialogStub, InputText: InputTextStub } }
  });
}

/**
 * Type into the palette's input.
 * @param wrapper Mounted palette.
 * @param value Text to set.
 */
async function type(wrapper: ReturnType<typeof mountPalette>, value: string): Promise<void> {
  await wrapper.find('.palette-input').setValue(value);
}

describe('CommandPalette', () => {
  beforeEach(() => {
    resetCommandRegistry();
    history.recordCommandHistory.mockClear();
    history.recentCommandHistory.mockReset().mockResolvedValue([]);
    skillsBridge.listSkills.mockReset().mockResolvedValue({ skills: [] });
    messages.getSessionList.mockReset().mockResolvedValue([]);
  });

  it('renders one row per registered command, grouped by section', () => {
    registerStaticBatch([
      command({ id: 'session.new', label: '新建会话', section: 'session' }),
      command({ id: 'toolbar.browser', label: '浏览器', section: 'navigate' }),
      command({ id: 'toolbar.skills', label: '技能', section: 'tools' })
    ]);

    const wrapper = mountPalette();
    const rows = wrapper.findAll('[data-command-id]');

    expect(rows.map(row => row.attributes('data-command-id'))).toEqual([
      'session.new',
      'toolbar.browser',
      'toolbar.skills'
    ]);
    // Section headings resolve through i18n (zh in tests).
    expect(wrapper.text()).toContain('会话');
    expect(wrapper.text()).toContain('导航');
    expect(wrapper.text()).toContain('工具');
  });

  it('filters as the user types and highlights the match', async () => {
    registerStaticBatch([command({ id: 'a', label: 'System Settings' }), command({ id: 'b', label: 'Browser' })]);

    const wrapper = mountPalette();
    await type(wrapper, 'set');

    const rows = wrapper.findAll('[data-command-id]');
    expect(rows.map(row => row.attributes('data-command-id'))).toEqual(['a']);
    expect(wrapper.find('mark').text()).toBe('Set');
  });

  it('never injects a command label through v-html', async () => {
    registerStaticBatch([command({ id: 'x', label: '<img src=x onerror=alert(1)>' })]);

    const wrapper = mountPalette();
    await type(wrapper, 'img');

    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('mark').text()).toBe('img');
  });

  it('shows the empty state when nothing matches (and Enter is a no-op)', async () => {
    registerStaticBatch([command({ id: 'a', label: 'Browser' })]);

    const wrapper = mountPalette();
    await type(wrapper, 'zzz');

    expect(wrapper.findAll('[data-command-id]')).toHaveLength(0);
    expect(wrapper.find('[data-test="command-palette-empty"]').text()).toBe('无匹配命令');

    await wrapper.find('.palette-input').trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('open-tab')).toBeUndefined();
  });

  it('walks the flat list with the arrows and runs the highlighted row on Enter', async () => {
    const first = command({ id: 'a', label: 'Alpha', section: 'session' });
    const second = command({ id: 'b', label: 'Beta', section: 'navigate' });
    const third = command({ id: 'c', label: 'Gamma', section: 'tools' });
    registerStaticBatch([first, second, third]);

    const wrapper = mountPalette();
    const input = wrapper.find('.palette-input');

    await input.trigger('keydown', { key: 'ArrowDown' });
    await input.trigger('keydown', { key: 'ArrowDown' });
    await input.trigger('keydown', { key: 'Enter' });

    expect(first.handler).not.toHaveBeenCalled();
    expect(second.handler).not.toHaveBeenCalled();
    expect(third.handler).toHaveBeenCalledTimes(1);
    expect(history.recordCommandHistory).toHaveBeenCalledWith(third);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('clamps the cursor at both ends of the list', async () => {
    const only = command({ id: 'a', label: 'Alpha' });
    registerStaticBatch([only]);

    const wrapper = mountPalette();
    const input = wrapper.find('.palette-input');
    await input.trigger('keydown', { key: 'ArrowUp' });
    await input.trigger('keydown', { key: 'ArrowDown' });
    await input.trigger('keydown', { key: 'ArrowDown' });
    await input.trigger('keydown', { key: 'Enter' });

    expect(only.handler).toHaveBeenCalledTimes(1);
  });

  it('runs a clicked row and closes', async () => {
    const entry = command({ id: 'a', label: 'Alpha' });
    registerStaticBatch([entry]);

    const wrapper = mountPalette();
    await wrapper.find('[data-command-id="a"]').trigger('click');

    expect(entry.handler).toHaveBeenCalledTimes(1);
    expect(history.recordCommandHistory).toHaveBeenCalledWith(entry);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('closes on Escape', async () => {
    registerStaticBatch([command({ id: 'a', label: 'Alpha' })]);
    const wrapper = mountPalette();

    await wrapper.find('.palette-input').trigger('keydown', { key: 'Escape' });
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('a new query puts the cursor back on the top match', async () => {
    const first = command({ id: 'a', label: 'Alpha' });
    const second = command({ id: 'ab', label: 'Alphabet' });
    registerStaticBatch([first, second]);

    const wrapper = mountPalette();
    const input = wrapper.find('.palette-input');
    await input.trigger('keydown', { key: 'ArrowDown' });
    await type(wrapper, 'alpha');
    await input.trigger('keydown', { key: 'Enter' });

    // 'alpha' matches 'Alpha' exactly (1000) and 'Alphabet' by prefix (493).
    expect(first.handler).toHaveBeenCalledTimes(1);
    expect(second.handler).not.toHaveBeenCalled();
  });

  it('shows the recent history on open and runs a still-registered command', async () => {
    const entry = command({ id: 'toolbar.skills', label: '技能' });
    registerStaticBatch([entry]);
    history.recentCommandHistory.mockResolvedValue([
      { id: 1, commandId: 'toolbar.skills', label: '技能', icon: 'pi pi-bolt', executedAt: 1 },
      { id: 2, commandId: 'gone.command', label: '已删除的命令', executedAt: 2 }
    ]);

    const wrapper = mountPalette();
    wrapper.findComponent({ name: 'Dialog' }).vm.$emit('show');
    await flushPromises();

    const recent = wrapper.find('[data-test="command-palette-recent"]');
    expect(recent.text()).toContain('最近使用');
    expect(recent.text()).toContain('技能');
    expect(recent.text()).toContain('已删除的命令');
    // The empty query shows history INSTEAD of the full list.
    expect(wrapper.findAll('[data-command-id]')).toHaveLength(0);

    // The dead row is inert: nothing to execute.
    await recent.findAll('button')[1]!.trigger('click');
    expect(wrapper.emitted('update:visible')).toBeUndefined();

    // The live row closes the panel and runs its handler.
    await recent.findAll('button')[0]!.trigger('click');
    expect(entry.handler).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('pulls skills and sessions on open and routes a skill row to the skill tab', async () => {
    skillsBridge.listSkills.mockResolvedValue({
      skills: [{ name: 'pdf-tools', description: 'PDF 处理', category: 'docs' }]
    });
    messages.getSessionList.mockResolvedValue([
      { id: 'sid-1', title: '会话一' },
      { id: 'sid-2', title: '会话二' }
    ]);

    const wrapper = mountPalette();
    wrapper.findComponent({ name: 'Dialog' }).vm.$emit('show');
    await flushPromises();

    await type(wrapper, 'pdf');
    const rows = wrapper.findAll('[data-command-id]');
    expect(rows.map(row => row.attributes('data-command-id'))).toEqual(['skill.pdf-tools']);

    await rows[0]!.trigger('click');
    expect(wrapper.emitted('open-tab')).toEqual([['skills']]);

    // Sessions joined the registry too (one dynamic list): the id is a keyword,
    // so a session can be found by title OR by id.
    await type(wrapper, 'sid-2');
    const sessionRows = wrapper.findAll('[data-command-id]');
    expect(sessionRows.map(row => row.attributes('data-command-id'))).toEqual(['session.switch.sid-2']);
    await sessionRows[0]!.trigger('click');
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });
});
