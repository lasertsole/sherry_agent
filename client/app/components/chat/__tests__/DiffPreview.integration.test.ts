import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import DiffPreview from '../DiffPreview.vue';
import { parseUnifiedDiff } from '~/utils/tool-diff';

/** Pure presentation: parse a real-shaped diff and assert the rendered rows. */
const SAMPLE = [
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,4 +1,5 @@',
  ' import os',
  '-print("old")',
  '+print("new")',
  '+',
  ' if __name__:'
].join('\n');

const mountPreview = (diffText: string, newFile = false) =>
  mount(DiffPreview, { props: { diff: parseUnifiedDiff(diffText)!, newFile } });

/**
 * The row `<div>`s under the root: row 0 is the stats header.
 * @param wrapper
 */
function rows(wrapper: ReturnType<typeof mountPreview>) {
  return wrapper.findAll('[data-test="tool-diff"] > div');
}

describe('DiffPreview', () => {
  it('renders the header stats and one row per classified line', () => {
    const all = rows(mountPreview(SAMPLE));

    expect(all.length).toBe(7); // header + hunk + 2 context + 1 removed + 2 added
    expect(all[0].text()).toContain('+2');
    expect(all[0].text()).toContain('−1');
  });

  it('styles the hunk separator blue and keeps the @@ line', () => {
    const all = rows(mountPreview(SAMPLE));

    expect(all[1].classes().join(' ')).toContain('bg-blue-50');
    expect(all[1].text()).toContain('@@ -1,4 +1,5 @@');
  });

  it('paints added green / removed red / context plain', () => {
    const all = rows(mountPreview(SAMPLE));

    expect(all[2].classes().join(' ')).not.toContain('bg-green-50'); // context
    expect(all[3].classes().join(' ')).toContain('bg-red-50');
    expect(all[4].classes().join(' ')).toContain('bg-green-50');
    expect(all[5].classes().join(' ')).toContain('bg-green-50');
    // The blank added line keeps its row (length-1 `+` line).
    expect(all[5].text()).not.toContain('print');
  });

  it('carries the line numbers in the two gutters', () => {
    const all = rows(mountPreview(SAMPLE));

    expect(all[2].text()).toContain('1'); // context: old 1 / new 1
    expect(all[3].text()).toContain('2'); // removed: old 2
    expect(all[4].text()).toContain('2'); // added: new 2
    expect(all[6].text()).toContain('3'); // trailing context: old 3 / new 4
  });

  it('draws the new-file badge (i18n) for an all-added preview', () => {
    const wrapper = mountPreview('--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n+one\n+two', true);

    expect(wrapper.text()).toContain('新建文件');
    expect(wrapper.text()).toContain('+2');
  });
});
