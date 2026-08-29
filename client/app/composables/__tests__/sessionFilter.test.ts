import { describe, it, expect } from 'vitest';
import { parseSessionCreateTime, matchesSessionFilter, filterSessions } from '../sessionFilter';

/** 本地占位会话格式（SessionSidebar.handleCreateSession 写入的格式） */
const LOCAL_FORMAT = 'YYYY-MM-DD HH:mm';

/** 构造一个最小会话记录（与 SessionRecord 的筛选相关字段一致） */
function makeSession(title: string, createTime: string) {
  return { id: `${title}-${createTime}`, title, createTime };
}

describe('parseSessionCreateTime', () => {
  it('parses the compact 14-digit server format (YYYYMMDDHHmmss)', () => {
    const parsed = parseSessionCreateTime('20260621004725');
    expect(parsed).not.toBeNull();
    expect(parsed!.format('YYYY-MM-DD HH:mm:ss')).toBe('2026-06-21 00:47:25');
  });

  it('parses the local placeholder format (YYYY-MM-DD HH:mm)', () => {
    const parsed = parseSessionCreateTime('2026-06-21 00:47');
    expect(parsed).not.toBeNull();
    expect(parsed!.format(LOCAL_FORMAT)).toBe('2026-06-21 00:47');
  });

  it('falls back to tolerant ISO parsing', () => {
    const parsed = parseSessionCreateTime('2026-06-21T08:30:00');
    expect(parsed).not.toBeNull();
    expect(parsed!.format('YYYY-MM-DD HH:mm')).toBe('2026-06-21 08:30');
  });

  it('returns null for invalid or empty input', () => {
    expect(parseSessionCreateTime('')).toBeNull();
    expect(parseSessionCreateTime('not-a-date')).toBeNull();
    expect(parseSessionCreateTime('20261321004725')).toBeNull(); // 13th month in compact format
  });
});

describe('matchesSessionFilter', () => {
  const item = makeSession('Detective Notes', '20260621004725');

  it('matches case-insensitively on title and trims the keyword', () => {
    expect(matchesSessionFilter(item, 'detective', null)).toBe(true);
    expect(matchesSessionFilter(item, 'DETECTIVE', null)).toBe(true);
    expect(matchesSessionFilter(item, '  detective  ', null)).toBe(true);
    expect(matchesSessionFilter(item, 'missing', null)).toBe(false);
  });

  it('treats empty/whitespace keyword as no keyword filter', () => {
    expect(matchesSessionFilter(item, '', null)).toBe(true);
    expect(matchesSessionFilter(item, '   ', null)).toBe(true);
  });

  it('matches within the full [start, end] day range inclusively', () => {
    const range = [new Date(2026, 5, 20), new Date(2026, 5, 22)];
    expect(matchesSessionFilter(item, '', range)).toBe(true);
    // 边界：会话创建当天的 00:00 与 23:59:59.999 均应包含。
    expect(matchesSessionFilter(makeSession('t', '20260620000000'), '', range)).toBe(true);
    expect(matchesSessionFilter(makeSession('t', '20260622235959'), '', range)).toBe(true);
    expect(matchesSessionFilter(makeSession('t', '20260619235959'), '', range)).toBe(false);
    expect(matchesSessionFilter(makeSession('t', '20260623000000'), '', range)).toBe(false);
  });

  it('combines keyword AND date range', () => {
    const range = [new Date(2026, 5, 21), new Date(2026, 5, 21)];
    expect(matchesSessionFilter(item, 'detective', range)).toBe(true);
    // 日期命中但关键字未命中 → 排除。
    expect(matchesSessionFilter(item, 'missing', range)).toBe(false);
    // 关键字命中但日期未命中 → 排除。
    expect(matchesSessionFilter(makeSession('Detective Notes', '20250101000000'), 'detective', range)).toBe(false);
  });

  it('treats a partial range [start] as lower-bound only', () => {
    const range = [new Date(2026, 5, 21)];
    expect(matchesSessionFilter(item, '', range)).toBe(true);
    expect(matchesSessionFilter(makeSession('t', '20250101000000'), '', range)).toBe(false);
  });

  it('excludes unparseable createTime only when a date filter is active', () => {
    const unparseable = makeSession('t', 'garbage');
    expect(matchesSessionFilter(unparseable, '', null)).toBe(true);
    expect(matchesSessionFilter(unparseable, 't', null)).toBe(true);
    expect(matchesSessionFilter(unparseable, '', [new Date(2026, 5, 21)])).toBe(false);
  });

  it('passes through when range is null or contains no valid dates', () => {
    expect(matchesSessionFilter(item, '', null)).toBe(true);
    expect(matchesSessionFilter(item, '', [])).toBe(true);
    expect(matchesSessionFilter(item, '', [null as unknown as Date])).toBe(true);
  });
});

describe('filterSessions', () => {
  const list = [
    makeSession('Detective Notes', '20260621004725'),
    makeSession('Diary 6月', '2026-06-21 00:47'),
    makeSession('Old memory', '20250101000000'),
    makeSession('Broken', 'garbage')
  ];

  it('returns the same list reference when no filters are active', () => {
    expect(filterSessions(list, '', null)).toBe(list);
    expect(filterSessions(list, '   ', [])).toBe(list);
  });

  it('filters by keyword only', () => {
    expect(filterSessions(list, 'detective', null)).toEqual([list[0]]);
    // 两种 createTime 格式均按日期语义参与关键字筛选无关，这里验证标题匹配。
    expect(filterSessions(list, '6月', null)).toEqual([list[1]]);
  });

  it('filters by date range only (mixed createTime formats)', () => {
    const range = [new Date(2026, 5, 21), new Date(2026, 5, 21)];
    expect(filterSessions(list, '', range)).toEqual([list[0], list[1]]);
  });

  it('combines keyword AND date range', () => {
    const range = [new Date(2026, 5, 21), new Date(2026, 5, 21)];
    expect(filterSessions(list, 'detective', range)).toEqual([list[0]]);
    expect(filterSessions(list, 'old', range)).toEqual([]);
  });
});
