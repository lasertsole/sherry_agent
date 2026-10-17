/**
 * Browser visit history (real Dexie over fake-indexeddb).
 *
 * Contract: every navigation records one row; a repeat visit to the same URL
 * inside the dedupe window UPDATES the newest row instead of appending; the
 * table is capped (oldest pruned) and the search matches URL or title
 * (newest first), falling back to the recent list for an empty query.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach } from 'vitest';
import { db } from '../db';
import {
  DEDUPE_WINDOW_MS,
  MAX_VISITS,
  clearBrowserVisits,
  recentBrowserVisits,
  recordBrowserVisit,
  searchBrowserVisits,
  visitAge
} from '../use-browser-history';

describe('browser visit history', () => {
  beforeEach(async () => {
    await db.browserVisits.clear();
  });

  it('records one row per navigation, keeping the session of the panel key', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com', 'Example');
    const rows = await recentBrowserVisits();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      sessionId: 'sid-1',
      url: 'https://example.com',
      title: 'Example'
    });
  });

  it('ignores blank and about:blank navigations', async () => {
    await recordBrowserVisit('sid-1::browser#1', '');
    await recordBrowserVisit('sid-1::browser#1', 'about:blank');
    expect(await db.browserVisits.count()).toBe(0);
  });

  it('updates the newest row for the same URL inside the dedupe window', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com');
    const first = (await recentBrowserVisits())[0]!;
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com', 'Late title');

    const rows = await recentBrowserVisits();
    expect(rows).toHaveLength(1);
    expect(rows[0]!.id).toBe(first.id);
    expect(rows[0]!.title).toBe('Late title');
  });

  it('appends a new row once the window has passed', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com');
    // Age the stored row past the dedupe window instead of sleeping.
    const row = (await recentBrowserVisits())[0]!;
    await db.browserVisits.update(row.id!, { visitedAt: Date.now() - DEDUPE_WINDOW_MS - 1000 });

    await recordBrowserVisit('sid-1::browser#1', 'https://example.com');

    expect(await db.browserVisits.count()).toBe(2);
  });

  it('never drops a title the new visit did not carry', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com', 'Known');
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com');
    const rows = await recentBrowserVisits();
    expect(rows[0]!.title).toBe('Known');
  });

  it('prunes the oldest rows past the cap', async () => {
    // Plant MAX_VISITS + 3 rows directly (faster than 500 navigations).
    const base = Date.now() - MAX_VISITS * 1000;
    await db.browserVisits.bulkAdd(
      Array.from({ length: MAX_VISITS + 3 }, (_, index) => ({
        sessionId: 'sid-1',
        url: `https://example.com/${index}`,
        title: '',
        visitedAt: base + index * 1000
      }))
    );

    await recordBrowserVisit('sid-1::browser#1', 'https://fresh.example');

    expect(await db.browserVisits.count()).toBe(MAX_VISITS);
    const urls = (await db.browserVisits.toArray()).map(row => row.url);
    expect(urls).toContain('https://fresh.example');
    expect(urls).not.toContain('https://example.com/0');
  });

  it('returns the newest first, limited', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://a.example');
    await recordBrowserVisit('sid-1::browser#1', 'https://b.example');
    await recordBrowserVisit('sid-1::browser#1', 'https://c.example');
    const rows = await recentBrowserVisits(undefined, 2);
    expect(rows.map(row => row.url)).toEqual(['https://c.example', 'https://b.example']);
  });

  it('filters by session when asked', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://a.example');
    await recordBrowserVisit('sid-2::browser#1', 'https://b.example');
    const rows = await recentBrowserVisits('sid-2');
    expect(rows.map(row => row.url)).toEqual(['https://b.example']);
  });

  it('matches the query against URL and title, newest first', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://docs.python.org/3/', 'asyncio docs');
    await recordBrowserVisit('sid-1::browser#1', 'https://github.com/langchain', 'langgraph');
    await recordBrowserVisit('sid-1::browser#1', 'https://example.com');

    expect((await searchBrowserVisits('python')).map(row => row.url)).toEqual(['https://docs.python.org/3/']);
    expect((await searchBrowserVisits('LANGGRAPH')).map(row => row.url)).toEqual(['https://github.com/langchain']);
    expect(await searchBrowserVisits('nope')).toEqual([]);
  });

  it('falls back to the recent list for an empty query', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://a.example');
    expect((await searchBrowserVisits('  ')).map(row => row.url)).toEqual(['https://a.example']);
  });

  it('scopes the search to one session when given', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://a.example', 'shared title');
    await recordBrowserVisit('sid-2::browser#1', 'https://b.example', 'shared title');
    const rows = await searchBrowserVisits('shared', 'sid-2');
    expect(rows.map(row => row.url)).toEqual(['https://b.example']);
  });

  it('clears everything', async () => {
    await recordBrowserVisit('sid-1::browser#1', 'https://a.example');
    await clearBrowserVisits();
    expect(await db.browserVisits.count()).toBe(0);
  });
});

describe('visitAge', () => {
  const now = 1_700_000_000_000;

  it('buckets seconds into "now"', () => {
    expect(visitAge(now - 5_000, now)).toEqual({ unit: 'now', value: 0 });
  });

  it('buckets minutes, hours and days', () => {
    expect(visitAge(now - 3 * 60_000, now)).toEqual({ unit: 'minutes', value: 3 });
    expect(visitAge(now - 5 * 3_600_000, now)).toEqual({ unit: 'hours', value: 5 });
    expect(visitAge(now - 3 * 86_400_000, now)).toEqual({ unit: 'days', value: 3 });
  });

  it('clamps a future timestamp to "now"', () => {
    expect(visitAge(now + 60_000, now)).toEqual({ unit: 'now', value: 0 });
  });
});
