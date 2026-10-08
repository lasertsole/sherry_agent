/**
 * The tool card's duration badge — the ONLY tool-card rendering point in the
 * client: the thinking stream and the conversation stream share this
 * component, so wiring it once covers both. The static guard at the bottom
 * turns red the day a second rendering point appears.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { mount } from '@vue/test-utils';
import ChatToolCard from '../ChatToolCard.vue';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '@/types/chat-role';

const toolMessage = (over: Partial<MessageItem> = {}): MessageItem => ({
  session_id: 's1',
  role: CHAT_ROLE.TOOL,
  content: 'result text',
  id: 7,
  turn_num: 1,
  timestamp: new Date().toISOString(),
  toolName: 'read_file',
  toolStatus: 'done',
  ...over
});

const mountCard = (message: MessageItem) =>
  mount(ChatToolCard, {
    props: {
      message,
      expanded: false,
      expandable: true,
      argsLabel: 'args',
      resultLabel: 'result',
      runningLabel: 'running',
      noOutputLabel: 'no output'
    }
  });

describe('ChatToolCard duration', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the measured duration once the tool settled', () => {
    const wrapper = mountCard(toolMessage({ toolDurationMs: 1234 }));
    expect(wrapper.text()).toContain('1.2s');
  });

  it('shows nothing when the duration is unknown', () => {
    const wrapper = mountCard(toolMessage({ toolDurationMs: undefined }));
    expect(wrapper.text()).not.toMatch(/\d+ms|\d+\.\d+s/);
  });

  it('counts up while running and freezes on the measured value', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-05T00:00:00Z'));
    const running = mountCard(toolMessage({ toolStatus: 'running', timestamp: '2026-10-05T00:00:00.000Z' }));

    expect(running.text()).toContain('00:00');
    await vi.advanceTimersByTimeAsync(3000);
    expect(running.text()).toContain('00:03');

    // Settled: the measured value replaces the ticker (the row is re-rendered
    // by the stream with the backend's number).
    await running.setProps({ message: toolMessage({ toolDurationMs: 850 }) });
    expect(running.text()).toContain('850ms');
    expect(running.text()).not.toContain('00:03');

    running.unmount();
  });
});

describe('render-point uniqueness guard', () => {
  /**
   * Every file under the client's app/ that renders `<ChatToolCard`.
   * @param dir
   */
  function filesRenderingToolCard(dir: string): string[] {
    const hits: string[] = [];
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) {
        if (entry === '__tests__' || entry === 'node_modules') continue;
        hits.push(...filesRenderingToolCard(path));
        continue;
      }
      if (!/\.(vue|ts)$/.test(entry)) continue;
      if (readFileSync(path, 'utf8').includes('<ChatToolCard')) hits.push(path);
    }
    return hits;
  }

  it('renders the tool card in exactly one place', () => {
    const hits = filesRenderingToolCard(join(process.cwd(), 'app'));

    // The thinking stream and the conversation stream are not two rendering
    // paths: ChatBox's single v-for interleaves them through this one card.
    // A second rendering point must wire the duration too — this test is what
    // reminds whoever adds it.
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('ChatBox.vue');
  });
});
