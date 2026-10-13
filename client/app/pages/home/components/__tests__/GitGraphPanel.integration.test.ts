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

// Every `.show(event)` the panel anchors the popup with (see the stub below).
const menuShots = vi.hoisted(() => [] as MouseEvent[]);

const bridge = vi.hoisted(() => ({
  fetchGitGraph: vi.fn(),
  fetchCommitFiles: vi.fn(),
  checkoutGitRef: vi.fn()
}));
vi.mock('~/composables/bridge/git', () => bridge);

// The component's bare `toastError`/`toastSuccess` resolve to an injected import
// of this module (not a runtime global), so intercepting them needs a mock.
const toasts = vi.hoisted(() => ({ toastError: vi.fn(), toastSuccess: vi.fn() }));
vi.mock('~/composables/toast', () => toasts);

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
    branches: [],
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
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  // The popup is only inspected through its `model` (items + commands) and the
  // events the panel anchors it with (`.show(event)`): the stub records both.
  ContextMenu: {
    name: 'ContextMenu',
    props: ['model'],
    setup() {
      return {
        show: (event: MouseEvent) => {
          menuShots.push(event);
        }
      };
    },
    template: '<div data-test="git-menu" />'
  }
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

  it('loads the next page when the list is scrolled near its end', async () => {
    const wrapper = await mountPanel([
      page({ commits: [commit('aaaa1111')], hasMore: true }),
      page({ commits: [commit('bbbb2222')], hasMore: false })
    ]);

    expect(wrapper.findAll('[data-test^="git-commit-"]')).toHaveLength(1);
    const scroller = wrapper.get('[data-test="git-scroll"]');
    // happy-dom has no layout: give the list a real height and park it at the end.
    Object.defineProperty(scroller.element, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(scroller.element, 'clientHeight', { value: 400, configurable: true });
    Object.defineProperty(scroller.element, 'scrollTop', { value: 700, configurable: true });
    await scroller.trigger('scroll');
    await flushPromises();

    const hashes = wrapper.findAll('[data-test^="git-commit-"]').map(row => row.attributes('data-test'));
    expect(hashes).toEqual(['git-commit-aaaa1111', 'git-commit-bbbb2222']);
    expect(wrapper.find('[data-test="git-more"]').exists()).toBe(false);
    // The second call asked for the offset after what is already loaded.
    expect(bridge.fetchGitGraph).toHaveBeenLastCalledWith('sid-1', { limit: 40, skip: 1 });
  });

  it('does not load a page while the list is still far from its end', async () => {
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111')], hasMore: true })]);

    const scroller = wrapper.get('[data-test="git-scroll"]');
    Object.defineProperty(scroller.element, 'scrollHeight', { value: 4000, configurable: true });
    Object.defineProperty(scroller.element, 'clientHeight', { value: 400, configurable: true });
    Object.defineProperty(scroller.element, 'scrollTop', { value: 100, configurable: true });
    await scroller.trigger('scroll');
    await flushPromises();

    expect(bridge.fetchGitGraph).toHaveBeenCalledTimes(1);
  });

  it('offers a retry in the pager row when a page fails to load', async () => {
    const wrapper = await mountPanel([
      page({ commits: [commit('aaaa1111')], hasMore: true }),
      page({ commits: [commit('bbbb2222')], hasMore: false })
    ]);
    bridge.fetchGitGraph.mockRejectedValueOnce(new Error('boom'));
    const scroller = wrapper.get('[data-test="git-scroll"]');
    Object.defineProperty(scroller.element, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(scroller.element, 'clientHeight', { value: 400, configurable: true });
    Object.defineProperty(scroller.element, 'scrollTop', { value: 700, configurable: true });

    await scroller.trigger('scroll');
    await flushPromises();

    // The failure is visible and actionable instead of a silent dead end.
    const retry = wrapper.get('[data-test="git-more-retry"]');
    await retry.trigger('click');
    await flushPromises();

    expect(wrapper.findAll('[data-test^="git-commit-"]')).toHaveLength(2);
    expect(wrapper.find('[data-test="git-more"]').exists()).toBe(false);
  });

  it('opens the file list UNDER the clicked row, not at the end of the list', async () => {
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111'), commit('bbbb2222'), commit('cccc3333')] })]);

    await wrapper.get('[data-test="git-commit-bbbb2222"]').trigger('click');
    await flushPromises();

    const row = wrapper.get('[data-test="git-commit-bbbb2222"]');
    const detail = wrapper.get('[data-test="git-detail"]');
    // The drawer is the row's next sibling — an accordion, not a block parked
    // after the whole list.
    expect(row.element.nextElementSibling).toBe(detail.element);
    expect(detail.element.parentElement?.lastElementChild).not.toBe(detail.element);
    // And the last row still follows it.
    expect(wrapper.get('[data-test="git-commit-cccc3333"]').element.parentElement).toBe(detail.element.parentElement);
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
    expect(empty.find('[data-test="git-more"]').exists()).toBe(false);
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

  it('lists the local branches behind the header and checks one out on click', async () => {
    const wrapper = await mountPanel([
      page({ branch: 'main', branches: ['main', 'feature', 'dev'], commits: [commit('aaaa1111')] })
    ]);
    const switcher = wrapper.get('[data-test="git-branch"]');
    expect(switcher.element.tagName).toBe('BUTTON');
    expect(switcher.text()).toBe('main');

    await switcher.trigger('click');
    const items = wrapper.findComponent({ name: 'ContextMenu' }).props('model') as Array<Record<string, unknown>>;

    // Every local branch is offered, the current one ticked and inert.
    expect(items.map(item => item.label)).toEqual(['main', 'feature', 'dev']);
    expect(items[0]!.disabled).toBe(true);
    expect(items[0]!.icon).toBe('pi pi-check');
    expect(items[1]!.disabled).toBe(false);

    // Picking another branch checks it out and repaints from the answer.
    bridge.checkoutGitRef.mockResolvedValueOnce(
      page({ branch: 'feature', branches: ['main', 'feature', 'dev'], commits: [commit('aaaa1111')] })
    );
    (items[1]!.command as () => void)();
    await flushPromises();

    expect(bridge.checkoutGitRef).toHaveBeenCalledWith('sid-1', 'feature');
    expect(wrapper.get('[data-test="git-branch"]').text()).toBe('feature');
  });

  it('leaves the header inert when there is no repository or no branch', async () => {
    const wrapper = await mountPanel([page({ available: false, reason: 'not-a-repository', branches: [] })]);

    // No branches → no switcher: the label is plain text and clicking is a no-op.
    const label = wrapper.get('[data-test="git-branch"]');
    expect(label.attributes('disabled')).toBeDefined();
    await label.trigger('click');
    expect(bridge.checkoutGitRef).not.toHaveBeenCalled();
  });

  it("opens the row's action menu from the drawer's background, not from its files", async () => {
    const wrapper = await mountPanel([page({ commits: [commit('aaaa1111', ['bbbb2222'])] })]);
    bridge.fetchCommitFiles.mockResolvedValueOnce({
      hash: 'aaaa1111',
      short: 'aaaa1111',
      author: 'Tester',
      date: '2026-10-07T10:00:00+08:00',
      parents: ['bbbb2222'],
      subject: 'subject aaaa',
      files: [{ status: 'M', path: 'src/app.ts', old_path: '' }]
    });
    await wrapper.get('[data-test="git-commit-aaaa1111"]').trigger('click');
    await flushPromises();

    const menu = () => wrapper.findComponent({ name: 'ContextMenu' }).props('model') as Array<Record<string, unknown>>;
    menuShots.length = 0;
    const drawer = wrapper.get('[data-test="git-detail"]');

    // The reference: what the row's own right-click shows.
    const row = wrapper.get('[data-test="git-commit-aaaa1111"]');
    await row.trigger('contextmenu');
    const rightClickItems = menu().map(item => item.label);
    expect(rightClickItems.length).toBeGreaterThan(2);
    const afterRightClick = menuShots.length;

    // A click on a FILE keeps its own meaning (open the diff) — no menu.
    await wrapper.get('[data-test="git-file-M-src/app.ts"]').trigger('click');
    expect(menuShots.length).toBe(afterRightClick);

    // A click elsewhere in the drawer opens the SAME menu the right-click shows,
    // anchored at the row's RIGHT edge (the geometry the panel positions by).
    (row.element as HTMLElement).getBoundingClientRect = () =>
      ({ right: 400, top: 100, height: 22, left: 0, bottom: 122, width: 400, x: 0, y: 100 }) as DOMRect;
    await drawer.trigger('click');

    expect(menuShots.length).toBe(afterRightClick + 1);
    expect(menu().map(item => item.label)).toEqual(rightClickItems);
    expect(menuShots.at(-1)?.clientX).toBe(400);
    expect(menuShots.at(-1)?.clientY).toBe(111);
    // The click did not toggle the drawer shut either.
    expect(wrapper.find('[data-test="git-detail"]').exists()).toBe(true);
  });

  it('renders a page payload without the branches field at all', async () => {
    // A component instance kept across an HMR update can still hold an OLDER page
    // object: reading `branches.length` off it took the whole panel's render down,
    // which silently killed row clicks and branch switches until a full reload.
    const payload = page({ commits: [commit('aaaa1111')] });
    delete (payload as Record<string, unknown>).branches;

    const wrapper = await mountPanel([payload]);

    expect(wrapper.get('[data-test="git-branch"]').text()).toBe('main');
    expect(wrapper.get('[data-test="git-branch"]').attributes('disabled')).toBeDefined();
    expect(wrapper.findAll('[data-test^="git-commit-"]')).toHaveLength(1);
  });

  it("keeps the page and shows git's reason when a checkout is refused", async () => {
    toasts.toastError.mockClear();
    const wrapper = await mountPanel([
      page({ branch: 'main', branches: ['main', 'feature'], commits: [commit('aaaa1111')] })
    ]);
    bridge.checkoutGitRef.mockRejectedValueOnce(
      new Error('error: Your local changes to the following files would be overwritten by checkout: src/app.ts')
    );

    await wrapper.get('[data-test="git-branch"]').trigger('click');
    const items = wrapper.findComponent({ name: 'ContextMenu' }).props('model') as Array<Record<string, unknown>>;
    (items[1]!.command as () => void)();
    await flushPromises();

    expect(bridge.checkoutGitRef).toHaveBeenCalledWith('sid-1', 'feature');
    // The refusal names git's own reason as the toast's detail, and the panel
    // keeps rendering the page it had (a null here used to brick the render).
    expect(toasts.toastError).toHaveBeenCalledWith('切换分支失败', expect.stringContaining('would be overwritten'));
    expect(wrapper.get('[data-test="git-branch"]').text()).toBe('main');
    expect(wrapper.findAll('[data-test^="git-commit-"]')).toHaveLength(1);
  });
});
