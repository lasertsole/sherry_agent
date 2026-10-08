/**
 * The commit-diff tab: the two-column (old | new) alignment, the single-column
 * shape an added/deleted file gets, and the three non-render paths (binary,
 * too-large, truncated).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import GitDiffPanel from '@/pages/home/components/GitDiffPanel.vue';
import { useGitDiffStore } from '@/stores/git-diff';

const bridge = vi.hoisted(() => ({ fetchCommitDiff: vi.fn() }));
vi.mock('~/composables/bridge/git', () => bridge);

// The panel resolves the store through Nuxt's auto-import, which vitest does not
// inject: the global stub hands it the REAL store (the test drives that one).
vi.stubGlobal('useGitDiffStore', () => useGitDiffStore());

/**
 * A diff payload with defaults.
 * @param overrides
 */
function diff(overrides: Record<string, unknown> = {}) {
  return {
    hash: 'aaaa1111bbbb2222',
    path: 'src/app.ts',
    old_path: 'src/app.ts',
    status: 'M',
    old_label: 'bbbb2222:src/app.ts',
    new_label: 'aaaa1111:src/app.ts',
    rows: [
      { left: { n: 1, text: 'keep', kind: 'same' }, right: { n: 1, text: 'keep', kind: 'same' } },
      { left: { n: 2, text: 'old line', kind: 'remove' }, right: { n: 2, text: 'new line', kind: 'add' } }
    ],
    truncated: false,
    binary: false,
    notice: '',
    ...overrides
  };
}

const stubs = {
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' }
};

/**
 * Mount the panel with one open target.
 * @param payload The diff the bridge answers with.
 * @param target Overrides for the opened target.
 */
async function mountPanel(payload: Record<string, unknown>, target: Record<string, string> = {}) {
  bridge.fetchCommitDiff.mockResolvedValue(payload);
  const store = useGitDiffStore();
  store.open({
    sessionId: 'sid-1',
    hash: 'aaaa1111bbbb2222',
    short: 'aaaa1111',
    path: 'src/app.ts',
    subject: 'change things',
    ...target
  });
  const wrapper = mount(GitDiffPanel, { global: { stubs } });
  await flushPromises();
  return { wrapper, store };
}

describe('GitDiffPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('renders a modified file as two columns with red removals and green additions', async () => {
    const { wrapper } = await mountPanel(diff());

    const rows = wrapper.findAll('[data-test^="git-diff-row-"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.findAll('td')).toHaveLength(4);
    // The changed row: removed on the left, added on the right, side by side.
    const cells = rows[1]!.findAll('td');
    expect(cells[1]!.text()).toBe('old line');
    expect(cells[1]!.classes().join(' ')).toContain('bg-red-50');
    expect(cells[3]!.text()).toBe('new line');
    expect(cells[3]!.classes().join(' ')).toContain('bg-emerald-50');
    // Line numbers ride each side; both labels head the two columns.
    expect(cells[0]!.text()).toBe('2');
    expect(cells[2]!.text()).toBe('2');
    expect(wrapper.text()).toContain('bbbb2222:src/app.ts');
    expect(wrapper.text()).toContain('aaaa1111:src/app.ts');
    expect(bridge.fetchCommitDiff).toHaveBeenCalledWith('sid-1', 'aaaa1111bbbb2222', 'src/app.ts');
  });

  it('renders an added file as ONE column of additions', async () => {
    const { wrapper } = await mountPanel(
      diff({
        status: 'A',
        old_label: '',
        rows: [
          { left: null, right: { n: 1, text: 'fresh', kind: 'add' } },
          { left: null, right: { n: 2, text: 'more', kind: 'add' } }
        ]
      })
    );

    const rows = wrapper.findAll('[data-test^="git-diff-row-"]');
    expect(rows[0]!.findAll('td')).toHaveLength(2);
    const cell = rows[0]!.get('[data-test="git-diff-single-cell"]');
    expect(cell.text()).toBe('fresh');
    expect(cell.classes().join(' ')).toContain('bg-emerald-50');
    expect(wrapper.get('[data-test="git-diff-status"]').text()).toBe('A');
  });

  it('renders a deleted file as ONE column of removals', async () => {
    const { wrapper } = await mountPanel(
      diff({
        status: 'D',
        new_label: '',
        rows: [{ left: { n: 1, text: 'gone', kind: 'remove' }, right: null }]
      })
    );

    const rows = wrapper.findAll('[data-test^="git-diff-row-"]');
    expect(rows[0]!.findAll('td')).toHaveLength(2);
    expect(rows[0]!.get('[data-test="git-diff-single-cell"]').classes().join(' ')).toContain('bg-red-50');
  });

  it('names a rename by both paths', async () => {
    const { wrapper } = await mountPanel(diff({ status: 'R', old_path: 'src/old.ts', path: 'src/app.ts' }));

    expect(wrapper.get('[data-test="git-diff-path"]').text()).toBe('src/old.ts → src/app.ts');
  });

  it('flags a binary side instead of decoding it', async () => {
    const { wrapper } = await mountPanel(diff({ binary: true, rows: [] }));

    expect(wrapper.get('[data-test="git-diff-binary"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="git-diff-body"]').exists()).toBe(false);
  });

  it('says when a side is too large and when the diff was clipped', async () => {
    const large = await mountPanel(diff({ notice: 'too-large', rows: [] }));
    expect(large.wrapper.get('[data-test="git-diff-too-large"]').exists()).toBe(true);

    const clipped = await mountPanel(diff({ truncated: true }));
    expect(clipped.wrapper.get('[data-test="git-diff-truncated"]').text()).toContain('2');
  });

  it('keeps several files of one commit as tabs inside the panel', async () => {
    const { wrapper, store } = await mountPanel(diff());
    bridge.fetchCommitDiff.mockResolvedValue(
      diff({ path: 'src/other.ts', rows: [{ left: null, right: { n: 1, text: 'x', kind: 'add' } }] })
    );
    store.open({
      sessionId: 'sid-1',
      hash: 'aaaa1111bbbb2222',
      short: 'aaaa1111',
      path: 'src/other.ts',
      subject: 'change things'
    });
    await flushPromises();

    const tabs = wrapper.findAll('[data-test^="git-diff-tab-"]');
    expect(tabs.length).toBeGreaterThanOrEqual(2);
    expect(wrapper.get('[data-test="git-diff-path"]').text()).toBe('src/other.ts');

    // Switching back to the first tab re-reads it (no stale content).
    store.activate(0);
    await flushPromises();
    expect(bridge.fetchCommitDiff).toHaveBeenLastCalledWith('sid-1', 'aaaa1111bbbb2222', 'src/app.ts');
  });
});
