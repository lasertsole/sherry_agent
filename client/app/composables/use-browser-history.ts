/**
 * The browser panel's visit history (Dexie-backed, survives reload + restart).
 *
 * Recording is best-effort and never blocks navigation: a failed write is
 * logged and swallowed (history is a convenience, not part of the turn). A
 * repeat visit to the same URL within :data:`DEDUPE_WINDOW_MS` UPDATES the
 * newest row (title + timestamp) instead of appending — one navigation can
 * surface several `page` frames (redirects, sub-frames), and re-entering the
 * same page twice in a minute is one entry for the user's purposes too.
 *
 * The table is capped at :data:`MAX_VISITS` rows: the oldest are pruned on
 * write, so IndexedDB cannot grow without bound.
 *
 * @module composables/use-browser-history
 */
import type { BrowserVisit } from './db';
import { logUtil } from '~/utils/log';

// `db` (the Dexie instance) is used as a bare symbol: composable exports are
// auto-imported, and a value import of a sibling composable is lint-banned.

/** Same-URL visits inside this window collapse into the newest row. */
export const DEDUPE_WINDOW_MS = 30_000;

/** Soft cap on stored visits (oldest pruned on write). */
export const MAX_VISITS = 500;

/** How many rows the dropdown renders at most. */
export const MAX_SUGGESTIONS = 10;

/**
 * Record one navigation of a browser panel.
 *
 * Never throws: a rejected Dexie write only logs.
 * @param stateKey The panel's identity (``sessionId::instance``).
 * @param url The visited URL (already normalized by the caller).
 * @param title The page title when the runtime reported one.
 */
export async function recordBrowserVisit(stateKey: string, url: string, title?: string): Promise<void> {
  const trimmed = url.trim();
  if (!trimmed || trimmed === 'about:blank') return;
  const sessionId = stateKey.split('::')[0] ?? '';
  try {
    const newest = await db.browserVisits.where('url').equals(trimmed).reverse().sortBy('visitedAt');
    const latest = newest[0];
    if (latest && Date.now() - latest.visitedAt < DEDUPE_WINDOW_MS) {
      await db.browserVisits.update(latest.id!, {
        title: title || latest.title,
        visitedAt: Date.now(),
        sessionId
      });
      return;
    }
    await db.browserVisits.add({ sessionId, url: trimmed, title: title ?? '', visitedAt: Date.now() });
    const count = await db.browserVisits.count();
    if (count > MAX_VISITS) {
      const oldest = await db.browserVisits
        .orderBy('visitedAt')
        .limit(count - MAX_VISITS)
        .primaryKeys();
      await db.browserVisits.bulkDelete(oldest as number[]);
    }
  } catch (e) {
    logUtil.w('[browser-history] record failed:', e);
  }
}

/**
 * The most recent visits (the dropdown's empty-query list).
 * @param sessionId When given, only that session's visits are returned.
 * @param limit Maximum rows.
 */
export async function recentBrowserVisits(sessionId?: string, limit = MAX_SUGGESTIONS): Promise<BrowserVisit[]> {
  try {
    if (sessionId) {
      const rows = await db.browserVisits.where('sessionId').equals(sessionId).toArray();
      return rows.sort((a, b) => b.visitedAt - a.visitedAt).slice(0, limit);
    }
    return await db.browserVisits.orderBy('visitedAt').reverse().limit(limit).toArray();
  } catch (e) {
    logUtil.w('[browser-history] read failed:', e);
    return [];
  }
}

/**
 * Fuzzy-match visits by URL or title (case-insensitive substring), newest
 * first. An empty/short query falls back to the recent list, so the dropdown
 * has something to show the moment it opens.
 * @param query What the address bar currently holds.
 * @param sessionId When given, matches are restricted to that session.
 */
export async function searchBrowserVisits(query: string, sessionId?: string): Promise<BrowserVisit[]> {
  const needle = query.trim().toLowerCase();
  if (!needle) return recentBrowserVisits(sessionId);
  try {
    // Dexie has no LIKE: scan a bounded newest slice in memory instead of
    // loading the whole table.
    const pool = sessionId
      ? (await db.browserVisits.where('sessionId').equals(sessionId).toArray())
          .sort((a, b) => b.visitedAt - a.visitedAt)
          .slice(0, MAX_VISITS)
      : await db.browserVisits.orderBy('visitedAt').reverse().limit(MAX_VISITS).toArray();
    return pool
      .filter(v => v.url.toLowerCase().includes(needle) || v.title.toLowerCase().includes(needle))
      .slice(0, MAX_SUGGESTIONS);
  } catch (e) {
    logUtil.w('[browser-history] search failed:', e);
    return [];
  }
}

/** Empty the whole history (the dropdown's clear action). */
export async function clearBrowserVisits(): Promise<void> {
  try {
    await db.browserVisits.clear();
  } catch (e) {
    logUtil.w('[browser-history] clear failed:', e);
  }
}

/**
 * The relative age of a visit as a unit + value, so the caller formats it in
 * the user's language (never a pre-baked English string).
 * @param visitedAt Epoch ms.
 * @param now Injectable clock (tests).
 */
export function visitAge(
  visitedAt: number,
  now: number = Date.now()
): { unit: 'now' | 'minutes' | 'hours' | 'days'; value: number } {
  const seconds = Math.max(0, Math.round((now - visitedAt) / 1000));
  if (seconds < 60) return { unit: 'now', value: 0 };
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return { unit: 'minutes', value: minutes };
  const hours = Math.round(minutes / 60);
  if (hours < 24) return { unit: 'hours', value: hours };
  return { unit: 'days', value: Math.round(hours / 24) };
}
