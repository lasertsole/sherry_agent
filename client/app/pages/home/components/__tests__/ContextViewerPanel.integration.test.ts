/**
 * The session's context viewer tab: the system prompt the chain injected, the
 * tool definitions the model can call, and the live message list — read from
 * `GET /context/inspect` and refreshed while the tab is open.
 *
 * Clicking any token figure in the chat opens THIS tab (the store dedupes by
 * kind), so the suite pins both halves: the panel's three sections render the
 * payload, and the chat's meta click reaches the store without stacking tabs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const bridge = vi.hoisted(() => ({ fetchContextInspect: vi.fn() }));
vi.mock('~/composables/bridge/session', () => bridge);
// The panel imports `useRoute` from vue-router directly, so the module is mocked
// (a stubGlobal would never be consulted).
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { sid: 'sid-1' } }) }));

import ContextViewerPanel from '@/pages/home/components/ContextViewerPanel.vue';

const payload = () => ({
  window: 131072,
  system_prompt: 'SYSTEM PROMPT BODY',
  system_tokens: 1200,
  tools: [
    { name: 'read_file', description: 'Read a file', parameters: { type: 'object' } },
    { name: 'terminal', description: 'Run a command' }
  ],
  tool_tokens: 3400,
  tool_selection: true,
  messages: [
    { role: 'human', content: '你好', origin: 'user' },
    { role: 'ai', content: '', tool_calls: [{ name: 'read_file', args: '{"path":"a.py"}' }] },
    { role: 'tool', content: 'FILE BODY', tool_call_id: 'c1' },
    {
      role: 'human',
      content: 'MOVED',
      origin: 'project_dir',
      internal: true,
      truncated: true
    }
  ],
  message_tokens: 900,
  truncated: 1,
  state_error: ''
});

const stubs = {
  Button: { name: 'Button', props: ['icon', 'title'], template: '<button class="btn" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' }
};

async function mountPanel() {
  const wrapper = mount(ContextViewerPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('ContextViewerPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.fetchContextInspect.mockResolvedValue(payload());
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders the three context parts with their counts and token figures', async () => {
    const wrapper = await mountPanel();

    expect(bridge.fetchContextInspect).toHaveBeenCalledWith('sid-1');
    const system = wrapper.get('[data-test="context-section-system"]');
    expect(system.text()).toContain('系统提示词');
    expect(wrapper.get('[data-test="context-system-text"]').text()).toBe('SYSTEM PROMPT BODY');

    const tools = wrapper.get('[data-test="context-section-tools"]');
    expect(tools.text()).toContain('工具');
    expect(tools.text()).toContain('2 项');
    // The session's own tool selection is reported.
    expect(tools.text()).toContain('已按会话选择裁剪工具');

    const messages = wrapper.get('[data-test="context-section-messages"]');
    expect(messages.text()).toContain('消息队列');
    expect(messages.text()).toContain('4 项');
    expect(messages.text()).toContain('含截断内容');
  });

  it('shows the transcript with roles, carriers and tool calls', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.get('[data-test="context-message-0"]').text()).toContain('human');
    expect(wrapper.get('[data-test="context-message-0"]').text()).toContain('你好');
    // The injected carrier says where it came from.
    const carrier = wrapper.get('[data-test="context-message-3"]');
    expect(carrier.text()).toContain('origin=project_dir');
    expect(carrier.text()).toContain('truncated');
    // The tool call is listed next to the (empty) AI row.
    expect(wrapper.get('[data-test="context-message-1"]').text()).toContain('read_file');
    expect(wrapper.get('[data-test="context-message-2"]').text()).toContain('FILE BODY');
  });

  it('lists the tool definitions and opens a schema on demand', async () => {
    const wrapper = await mountPanel();

    // The tools section starts collapsed: its rows appear once it is opened.
    const toolsSection = wrapper.get('[data-test="context-section-tools"]');
    expect(wrapper.find('[data-test="context-tool-read_file"]').exists()).toBe(false);
    await toolsSection.trigger('click');

    expect(wrapper.get('[data-test="context-tool-read_file"]').text()).toContain('有参数');
    expect(wrapper.get('[data-test="context-tool-terminal"]').text()).toContain('无参数');
    expect(wrapper.text()).toContain('Read a file');
    await wrapper.get('[data-test="context-tool-read_file"]').trigger('click');
    expect(wrapper.text()).toContain('"type": "object"');
  });

  it('refreshes itself while it is open', async () => {
    vi.useFakeTimers();
    const wrapper = await mountPanel();
    expect(bridge.fetchContextInspect).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(3100);
    await flushPromises();
    expect(bridge.fetchContextInspect).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it('keeps the last snapshot when a refresh fails', async () => {
    const wrapper = await mountPanel();
    bridge.fetchContextInspect.mockRejectedValueOnce(new Error('boom'));

    await (wrapper.vm as unknown as { load: () => Promise<void> }).load();
    await flushPromises();

    expect(wrapper.get('[data-test="context-system-text"]').text()).toBe('SYSTEM PROMPT BODY');
  });

  it('surfaces a failed state read while still rendering the other parts', async () => {
    bridge.fetchContextInspect.mockResolvedValueOnce({
      ...payload(),
      messages: [],
      state_error: 'checkpoint down'
    });

    const wrapper = await mountPanel();

    expect(wrapper.get('[data-test="context-state-error"]').text()).toContain('checkpoint down');
    expect(wrapper.get('[data-test="context-system-text"]').text()).toBe('SYSTEM PROMPT BODY');
  });
});
