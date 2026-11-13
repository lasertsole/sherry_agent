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
      noOutputLabel: 'no output',
      trajectoryLabel: 'trajectory'
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

  it('emits open-trajectory from the header jump', async () => {
    const wrapper = mountCard(toolMessage());

    await wrapper.find('button[aria-label="trajectory"]').trigger('click');

    expect(wrapper.emitted('open-trajectory')).toHaveLength(1);
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

/**
 * The inline diff branches: patch_file / write_file results render as a
 * colored diff; every other tool (and every unparseable shape) keeps the
 * plain <pre> — fail-open, byte-identical for untouched tools.
 */
const DIFF_LINES = ['--- a/src/app.py', '+++ b/src/app.py', '@@ -1,3 +1,3 @@', ' import os', '-old()', '+new()'].join(
  '\n'
);
const patchMessage = () =>
  toolMessage({
    toolName: 'patch_file',
    toolResult: JSON.stringify({ success: true, path: 'src/app.py', diff: DIFF_LINES })
  });

const mountExpanded = (message: MessageItem) =>
  mount(ChatToolCard, {
    props: {
      message,
      expanded: true,
      expandable: true,
      argsLabel: 'args',
      resultLabel: 'result',
      runningLabel: 'running',
      noOutputLabel: 'no output',
      trajectoryLabel: 'trajectory'
    }
  });

describe('ChatToolCard inline diff', () => {
  it('renders a patch_file result as a diff, not a <pre>', () => {
    const wrapper = mountExpanded(patchMessage());

    expect(wrapper.find('[data-test="tool-diff"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="file-viewer-pre"], pre').exists()).toBe(false);
    expect(wrapper.text()).toContain('new()');
  });

  it('keeps the <pre> for other tools (F1)', () => {
    const wrapper = mountExpanded(
      toolMessage({ toolName: 'read_file', toolResult: JSON.stringify({ diff: DIFF_LINES }) })
    );

    expect(wrapper.find('[data-test="tool-diff"]').exists()).toBe(false);
    expect(wrapper.find('pre').exists()).toBe(true);
  });

  it('falls back to the <pre> when the payload is malformed (F2)', () => {
    const wrapper = mountExpanded(toolMessage({ toolName: 'patch_file', toolResult: '{"diff": oops' }));

    expect(wrapper.find('[data-test="tool-diff"]').exists()).toBe(false);
    expect(wrapper.find('pre').exists()).toBe(true);
  });

  it('shows the new-file badge and an all-green preview for a creation', () => {
    const wrapper = mountExpanded(
      toolMessage({
        toolName: 'write_file',
        toolArgs: { file_path: 'new.py', text: 'print(1)\nprint(2)' },
        toolResult: JSON.stringify({ success: true, path: 'new.py', isNew: true, diff: null })
      })
    );

    expect(wrapper.find('[data-test="tool-diff"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('新建文件');
    expect(wrapper.text()).toContain('print(2)');
    // The whole preview is green (no removed rows).
    expect(wrapper.text()).toContain('−0');
  });
});

/**
 * Artifact images: a tool result carrying `sherry-artifact://` references
 * renders as thumbnails (never raw text), and the thumbnails are visible even
 * while the card is collapsed so a generated picture cannot hide behind the
 * fold. Clicking one opens the shared gallery.
 */
describe('ChatToolCard artifact images', () => {
  const URI = 'sherry-artifact://s1/image-0123456789abcdef.png';
  const OTHER = 'sherry-artifact://s1/image-fedcba9876543210.png';
  const URL = 'http://localhost:8080/artifacts/s1/image-0123456789abcdef.png';

  const mountExpanded = (message: MessageItem) =>
    mount(ChatToolCard, {
      props: {
        message,
        expanded: true,
        expandable: true,
        argsLabel: 'args',
        resultLabel: 'result',
        runningLabel: 'running',
        noOutputLabel: 'no output',
        trajectoryLabel: 'trajectory'
      }
    });

  it('renders thumbnails while the card is collapsed', () => {
    const wrapper = mountCard(toolMessage({ toolResult: URI }));

    expect(wrapper.find('[data-test="artifact-image-gallery"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="artifact-image-thumb"] img').attributes('src')).toBe(URL);
  });

  it('replaces the plain-text result with the grid when expanded', () => {
    const wrapper = mountExpanded(toolMessage({ toolResult: URI }));

    expect(wrapper.find('[data-test="artifact-image-gallery"]').exists()).toBe(true);
    expect(wrapper.find('pre').exists()).toBe(false);
  });

  it('keeps prose beside the images readable on expand', () => {
    const wrapper = mountExpanded(
      toolMessage({
        toolResult: `${URI}\n[warn] 1 generated image(s) could not be persisted and were discarded.`
      })
    );

    expect(wrapper.find('[data-test="artifact-image-gallery"]').exists()).toBe(true);
    expect(wrapper.get('pre').text()).toContain('[warn] 1 generated image(s)');
  });

  it('renders one thumbnail per distinct reference', () => {
    const wrapper = mountExpanded(toolMessage({ toolResult: `${URI}\n${OTHER}\n${URI}` }));

    expect(wrapper.findAll('[data-test="artifact-image-thumb"]')).toHaveLength(2);
  });

  it('keeps the plain <pre> for results without artifact references', () => {
    const wrapper = mountExpanded(toolMessage({ toolResult: 'plain output' }));

    expect(wrapper.find('[data-test="artifact-image-gallery"]').exists()).toBe(false);
    expect(wrapper.get('pre').text()).toContain('plain output');
  });

  it('opens the shared gallery on the clicked image', async () => {
    const wrapper = mountExpanded(toolMessage({ toolResult: `${URI}\n${OTHER}` }));
    const preview = await import('../../../composables/useImagePreview');
    const gallery = preview.useImagePreview();

    await wrapper.findAll('[data-test="artifact-image-thumb"]')[1].trigger('click');

    expect(gallery.isPreviewVisible.value).toBe(true);
    expect(gallery.galleryItems.value).toEqual([URL, 'http://localhost:8080/artifacts/s1/image-fedcba9876543210.png']);
    expect(gallery.currentIndex.value).toBe(1);
    gallery.closePreview();
  });
});
