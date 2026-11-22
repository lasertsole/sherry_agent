/**
 * S7 shared placeholder: a nav item whose backend/panel is not shipped yet must
 * still render a friendly "not implemented" message instead of a blank column.
 * Values are asserted against the four locale dictionaries so the copy cannot
 * silently drift out of one language.
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import NotImplementedPanel from '../NotImplementedPanel.vue';
import zh from '../../i18n/locales/zh.json';
import en from '../../i18n/locales/en.json';
import ja from '../../i18n/locales/ja.json';
import ko from '../../i18n/locales/ko.json';

type Dict = Record<string, unknown>;
const keyOf = (dict: Dict, path: string): unknown =>
  path
    .split('.')
    .reduce<unknown>((node, part) => (node && typeof node === 'object' ? (node as Dict)[part] : undefined), dict);

describe('NotImplementedPanel', () => {
  it('renders the placeholder icon and both message lines', () => {
    const wrapper = mount(NotImplementedPanel);
    expect(wrapper.get('[data-test="not-implemented-panel"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="placeholder-icon"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="not-implemented-title"]').text()).toBeTruthy();
    expect(wrapper.get('[data-test="coming-soon-hint"]').text()).toBeTruthy();
  });

  it('all four locales carry the planned placeholder copy', () => {
    const expected: Record<string, { notImplemented: string; comingSoon: string }> = {
      zh: { notImplemented: '暂未实现', comingSoon: '敬请期待' },
      en: { notImplemented: 'Not yet implemented', comingSoon: 'Coming soon' },
      ja: { notImplemented: '未実装です', comingSoon: '近日公開' },
      ko: { notImplemented: '아직 구현되지 않았습니다', comingSoon: '출시 예정' }
    };
    for (const [lang, dict] of Object.entries({ zh, en, ja, ko })) {
      expect(keyOf(dict as Dict, `common.notImplemented`)).toBe(expected[lang]!.notImplemented);
      expect(keyOf(dict as Dict, `common.comingSoon`)).toBe(expected[lang]!.comingSoon);
    }
  });
});
