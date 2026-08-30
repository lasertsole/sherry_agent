import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

/**
 * Pure filtering utilities for the session list (lets SessionSidebar filter the
 * already-loaded historyList locally on the client without issuing any requests).
 * Two optional conditions apply simultaneously (AND): keyword (title) and creation
 * date range.
 *
 * `createTime` comes in two formats (see SessionSidebar.loadSessionList /
 * handleCreateSession):
 * - Server sessions: compact 14-digit `YYYYMMDDHHmmss` (e.g. `20260621004725`);
 * - Local placeholder sessions: `YYYY-MM-DD HH:mm` (e.g. `2026-06-21 00:47`).
 * A lenient ISO fallback is also kept; when none of the three can parse, null is
 * returned.
 */

// The customParseFormat plugin must be registered before parsing so strings can be
// parsed with custom formats.
// common/utils.ts already registers it once on the app side; calling dayjs.extend
// repeatedly with the same plugin is idempotent (it only re-wraps prototype methods,
// behavior unchanged), and it is registered once more here at module load time,
// ensuring this module works when loaded standalone through any entry point
// (including unit tests).
dayjs.extend(customParseFormat);

/**
 * Parse a session creation time into a Dayjs object.
 *
 * Attempts in order (first two in strict mode to avoid false matches):
 * 1. Compact 14-digit `YYYYMMDDHHmmss` (server sessions);
 * 2. `YYYY-MM-DD HH:mm` (local placeholder sessions);
 * 3. Lenient ISO fallback (dayjs default parsing).
 *
 * @param raw Raw time string
 * @returns A Dayjs on success; null for empty/unparseable values
 */
export function parseSessionCreateTime(raw: string): Dayjs | null {
  if (!raw) return null;
  const compact = dayjs(raw, 'YYYYMMDDHHmmss', true);
  if (compact.isValid()) return compact;
  const local = dayjs(raw, 'YYYY-MM-DD HH:mm', true);
  if (local.isValid()) return local;
  // Pure digit strings are only allowed through the strict 14-digit compact format:
  // JS Date's lenient fallback parsing would "carry over" invalid compact strings
  // (e.g. month 13 in `20261321004725`) into seemingly valid dates (2027-01).
  if (/^\d+$/.test(raw.trim())) return null;
  const fallback = dayjs(raw);
  return fallback.isValid() ? fallback : null;
}

/** Whether at least one valid date exists in a date-range array (distinguishes "date filter disabled" from "enabled") */
function hasRangeValue(range: Date[] | null): boolean {
  return Array.isArray(range) && range.some(d => d != null);
}

/** Whether any filter condition is currently active (non-blank keyword, or date range containing a valid date) */
export function hasActiveSessionFilter(keyword: string, range: Date[] | null): boolean {
  return keyword.trim().length > 0 || hasRangeValue(range);
}

/**
 * Check whether a single session satisfies both the keyword and date-range filters
 * (AND).
 *
 * - Keyword: case-insensitive `includes` match; a blank keyword counts as disabled;
 * - Date range: covers the entire start day and the entire end day
 *   (startOf/endOf('day')); supports a full `[start, end]` and a partial `[start]`
 *   (lower bound only);
 * - When the date filter is enabled, sessions whose createTime cannot be parsed are
 *   excluded; when disabled, they pass through directly.
 *
 * @param item The session to check (must contain at least title and createTime)
 * @param keyword Title keyword (raw value; trimmed internally)
 * @param range Date range (value from PrimeVue Calendar range mode; may be null)
 */
export function matchesSessionFilter(
  item: { title: string; createTime: string },
  keyword: string,
  range: Date[] | null
): boolean {
  const kw = keyword.trim().toLowerCase();
  if (kw && !item.title.toLowerCase().includes(kw)) return false;

  if (!hasRangeValue(range)) return true;

  const created = parseSessionCreateTime(item.createTime);
  // When the date filter is enabled: an unparseable creation time cannot be
  // attributed to any day, so exclude it outright.
  if (!created) return false;

  const startRaw = Array.isArray(range) ? range[0] : null;
  const endRaw = Array.isArray(range) && range.length > 1 ? (range[1] ?? null) : null;
  // Partial range [start]: lower bound only (from 00:00:00 of the start day).
  if (startRaw && created.isBefore(dayjs(startRaw).startOf('day'))) return false;
  // Full range [start, end]: upper bound is 23:59:59.999 of the end day, covering the entire end day.
  if (endRaw && created.isAfter(dayjs(endRaw).endOf('day'))) return false;
  return true;
}

/**
 * Filter a session list: keyword and date range apply simultaneously (AND).
 *
 * @param list Session list
 * @param keyword Title keyword (raw value; trimmed internally)
 * @param range Date range (may be null)
 * @returns Returns the same array reference unchanged when both filters are
 *   disabled; otherwise returns a new filtered array
 */
export function filterSessions<T extends { title: string; createTime: string }>(
  list: T[],
  keyword: string,
  range: Date[] | null
): T[] {
  if (!hasActiveSessionFilter(keyword, range)) return list;
  return list.filter(item => matchesSessionFilter(item, keyword, range));
}
