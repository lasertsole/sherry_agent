/**
 * The left sidebar's git-graph panel: the lane layout, the row rendering, the
 * paging and the three empty states (not a repository / no git / no commits).
 *
 * The lane geometry is the part worth pinning: a linear history is one lane, a
 * second branch head opens a lane, and a merge draws an edge to its second
 * parent's lane.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import GitGraphPanel from '@/pages/home/components/GitGraphPanel.vue';

const bridge = vi.hoisted(() => ({ fetchGitGraph: vi.fn(), fetchCommitFiles: vi.fn() }));
vi.mock('~/composables/bridge/git', () => bridge);

const gitDiffStore = vi.hoisted(() => ({ open: vi.fn() }));
vi.stubGlobal('useGitDiffStore', () => gitDiffStore);
const sidebarStore = vi.hoisted(() => ({ openTab: vi.fn() }));
vi.stubGlobal('useRightSidebarStore', () => sidebarStore);

/**
 * One commit entry with the fields the panel reads.
 * @param hash
 * @param parents
 * @param overrides
 */
function commit(hash: string, parents: string[] = [], overrides: Partial<Record<string, unknown>> = {}) {
  return {
    hash,
    short: hash.slice(0, 8),
    parents,
    author: 'Tester',
    date: '2026-10-07T10:00:00+08:00',
    refs: [],
    subject: `subject ${hash.slice(0, 4)}`,
    ...overrides
  };
}

/**
 * A page payload with defaults.
 * @param overrides
 */
function page(overrides: Record<string, unknown> = {}) {
  return {
    root: '/tmp/project',
    source: 'session',
    available: true,
    reason: '',
    branch: 'main',
    detached: false,
    dirty: 0,
    dirty_capped: false,
    commits: [],
    hasMore: false,
    ...overrides
  };
}

const stubs = {
  Button: {
    name: 'Button',
    props: ['label', 'loading'],
    emits: ['click'],
    template: `<button class="btn" @click="$emit('click')">{{ label }}</button>`
  },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' }
};

/**
 * Mount the panel with the bridge answering *payloads* in order.
 * @param payloads One page payload per call (the last one repeats).
 */
async function mountPanel(payloads: Array<Record<string, unknown>>) {
  let call = 0;
  bridge.fetchGitGraph.mockImplementation(async () => {
    const payload = payloads[Math.min(call, payloads.length - 1)]!;
    call += 1;
    return payload;
  });
  const wrapper = mount(GitGraphPanel, {
    props: { sessionId: 'sid-1' },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

describe('GitGraphPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Rows are clickable everywhere in this suite: the file list answers empty
    // unless a test replaces it.
    bridge.fetchCommitFiles.mockResolvedValue({
      hash: 'aaaa1111',
      short: 'aaaa1111',
      author: 'Tester',
      date: '2026-10-07T10:00:00+08:00',
      parents: [],
      subject: 'subject',
      files: []
    });
  });

  it('draws a linear history in one lane and shows the branch header', async () => {
    const wrapper = await mountPanel([
      page({
        commits: [commit('aaaa1111', ['bbbb2222']), commit('bbbb2222', ['cccc3333']), commit('cccc3333')],
        dirty: 2
      })
    ]);

    expect(wrapper.get('[data-test="git-branch"]').text()).toBe('main');
    expect(wrapper.get('[data-test="git-dirty"]').text()).toContain('2');
    const rows = wrapper.findAll('[data-test^="git-commit-"]');
    expect(rows).toHaveLength(3);
    // Every row keeps a single lane: one vertical line plus the node dot
    // (VS Code's geometry: LANE_WIDTH 11 → 22px for one lane).
    const first = rows[0]!;
    expect(first.find('svg').attributes('width')).toBe('22');
    expect(first.find('svg').attributes('height')).toBe('22');
    expect(first.findAll('path')).toHaveLength(1);
    expect(first.find('circle').exists()).toBe(true);
  });

  it('opens a lane for a second branch head and draws the merge edge', async () => {
    // a = merge(f, b); f = feature; b = main tip — a classic two-lane shape.
    const wrapper = await mountPanel([
      page({
        commits: [
          commit('aaaa1111', ['ffff2222', 'bbbb3333']),
          commit('ffff2222', ['bbbb3333']),
          commit('bbbb3333', [])
        ]
      })
    ]);

    const rows = wrapper.findAll('[data-test^="git-commit-"]');
    // The merge row carries two lanes plus the edge to the second parent, and a
    // merge node is drawn as a ring (an outer circle + an inner dot).
    // Two segments: the lane running into the node plus the merge edge to the
    // lane the second parent opened (the edge is a two-arc curve).
    const mergePaths = rows[0]!.findAll('path').map(path => path.attributes('d')!);
    expect(mergePaths).toHaveLength(2);
    expect(mergePaths.some(d => d.includes('A 5 5'))).toBe(true);
    expect(Number(rows[0]!.find('svg').attributes('width'))).toBeGreaterThan(22);
    // A merge node is a ring: an outer circle (r=6) plus a small filled dot (r=3).
    const circles = rows[0]!.findAll('circle');
    expect(circles).toHaveLength(2);
    expect(circles[0]!.attributes('r')).toBe('6');
    expect(circles[1]!.attributes('r')).toBe('3');
    // The two heads below keep their own lanes (the merge target stays compact).
    expect(rows[2]!.findAll('path').length).toBeGreaterThanOrEqual(1);
  });

  it('expands one row at a time with the full hash and the parents', async () => {
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111', ['bbbb2222']), commit('bbbb2222')] })]);

    expect(wrapper.find('[data-test="git-detail"]').exists()).toBe(false);
    await wrapper.get('[data-test="git-commit-aaaa1111"]').trigger('click');
    const detail = wrapper.get('[data-test="git-detail"]');
    expect(detail.text()).toContain('aaaa1111');
    expect(detail.text()).toContain('bbbb2222');

    // Clicking another row moves the detail, clicking again closes it.
    await wrapper.get('[data-test="git-commit-bbbb2222"]').trigger('click');
    expect(wrapper.get('[data-test="git-detail"]').text()).toContain('bbbb2222');
    await wrapper.get('[data-test="git-commit-bbbb2222"]').trigger('click');
    expect(wrapper.find('[data-test="git-detail"]').exists()).toBe(false);
  });

  it('appends the next page and keeps the loaded commits', async () => {
    const wrapper = await mountPanel([
      page({ commits: [commit('aaaa1111')], hasMore: true }),
      page({ commits: [commit('bbbb2222')], hasMore: false })
    ]);

    expect(wrapper.findAll('[data-test^="git-commit-"]')).toHaveLength(1);
    const more = wrapper.get('[data-test="git-load-more"]');
    await more.trigger('click');
    await flushPromises();

    const hashes = wrapper.findAll('[data-test^="git-commit-"]').map(row => row.attributes('data-test'));
    expect(hashes).toEqual(['git-commit-aaaa1111', 'git-commit-bbbb2222']);
    expect(wrapper.find('[data-test="git-load-more"]').exists()).toBe(false);
    // The second call asked for the offset after what is already loaded.
    expect(bridge.fetchGitGraph).toHaveBeenLastCalledWith('sid-1', { limit: 40, skip: 1 });
  });

  it('renders one empty state per refusal', async () => {
    const unavailable = await mountPanel([page({ available: false, reason: 'not-a-repository' })]);
    expect(unavailable.get('[data-test="git-unavailable"]').text()).toContain('Git');

    const noGit = await mountPanel([page({ available: false, reason: 'git-unavailable' })]);
    expect(noGit.get('[data-test="git-unavailable"]').text()).not.toBe('');

    // An empty repository keeps its (unborn) branch name and says so in the body.
    const empty = await mountPanel([page({ commits: [], available: true })]);
    expect(empty.get('[data-test="git-empty"]').text()).toBe('暂无提交');
    expect(empty.get('[data-test="git-branch"]').text()).toBe('main');
    expect(empty.find('[data-test="git-load-more"]').exists()).toBe(false);
  });

  it('shows ref chips with one tint per kind', async () => {
    const wrapper = await mountPanel([
      page({
        commits: [
          commit('aaaa1111', [], {
            refs: [
              { kind: 'head', name: 'main' },
              { kind: 'tag', name: 'v1' },
              { kind: 'remote', name: 'origin/main' }
            ]
          })
        ]
      })
    ]);

    const chips = wrapper.findAll('[data-test="git-commit-aaaa1111"] [data-test^="git-ref-"]');
    expect(chips.map(chip => chip.text())).toEqual(['main', 'v1', 'origin/main']);
    const classes = chips.map(chip => chip.classes().join(' '));
    expect(classes[0]).toContain('bg-[#c1d6e5]');
    expect(classes[1]).toContain('amber');
    expect(classes[2]).toContain('text-gray-500');
  });

  it('expands a row into its file list and opens a file as a diff tab', async () => {
    bridge.fetchCommitFiles.mockResolvedValue({
      hash: 'aaaa1111',
      short: 'aaaa1111',
      author: 'Tester',
      date: '2026-10-07T10:00:00+08:00',
      parents: ['bbbb2222'],
      subject: 'change things',
      files: [
        { status: 'M', path: 'src/app.ts', old_path: '' },
        { status: 'A', path: 'src/new.ts', old_path: '' },
        { status: 'D', path: 'src/gone.ts', old_path: '' }
      ]
    });
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111', ['bbbb2222'])] })]);

    // Nothing is fetched before the row is opened.
    expect(bridge.fetchCommitFiles).not.toHaveBeenCalled();
    await wrapper.get('[data-test="git-commit-aaaa1111"]').trigger('click');
    await flushPromises();

    expect(bridge.fetchCommitFiles).toHaveBeenCalledWith('sid-1', 'aaaa1111');
    expect(wrapper.get('[data-test="git-file-M-src/app.ts"]').text()).toContain('src/app.ts');
    expect(wrapper.get('[data-test="git-file-A-src/new.ts"]').exists()).toBe(true);

    // Clicking a file opens its diff: the target reaches the diff store and the
    // right sidebar hosts it as a tab (the sidebar has no KeepAlive).
    await wrapper.get('[data-test="git-file-M-src/app.ts"]').trigger('click');
    expect(gitDiffStore.open).toHaveBeenCalledWith({
      sessionId: 'sid-1',
      hash: 'aaaa1111',
      short: 'aaaa1111',
      path: 'src/app.ts',
      subject: 'subject aaaa'
    });
    expect(sidebarStore.openTab).toHaveBeenCalledWith('gitDiff', {
      path: 'src/app.ts',
      hash: 'aaaa1111'
    });

    // Re-opening the same row re-uses the cached list (no second fetch).
    await wrapper.get('[data-test="git-commit-aaaa1111"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="git-commit-aaaa1111"]').trigger('click');
    await flushPromises();
    expect(bridge.fetchCommitFiles).toHaveBeenCalledTimes(1);
  });

  it('reloads when the session changes', async () => {
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111')] })]);
    expect(bridge.fetchGitGraph).toHaveBeenCalledTimes(1);

    await wrapper.setProps({ sessionId: 'sid-2' });
    await flushPromises();

    expect(bridge.fetchGitGraph).toHaveBeenCalledTimes(2);
    expect(bridge.fetchGitGraph).toHaveBeenLastCalledWith('sid-2', { limit: 40, skip: 0 });
  });
});
