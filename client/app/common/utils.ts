import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

// This plugin must be registered before strings in custom formats can be parsed
dayjs.extend(customParseFormat);

/**
 * Convert a compact time string (e.g. 20260621004725) into a target format
 * @param timeStr Compact time string, usually 14 digits
 * @param format The desired output format, e.g. 'YYYY-MM-DD HH:mm:ss'
 * @returns The formatted time string, or an empty string when the input is invalid
 */
/**
 * Elapsed time since a start timestamp (epoch ms), as `mm:ss` or `h:mm:ss`.
 * The ticking value comes from the caller (it re-renders on its own interval).
 * @param startedAtMs Epoch milliseconds, or null/undefined when it never started.
 * @param nowMs Current time in epoch milliseconds.
 */
export function formatElapsed(startedAtMs: number | null | undefined, nowMs: number): string {
  if (startedAtMs == null || Number.isNaN(Number(startedAtMs))) return '--:--';
  const seconds = Math.max(0, Math.floor((nowMs - Number(startedAtMs)) / 1000));
  const s = seconds % 60;
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600);
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/**
 * A tool's measured execution time, ZCode's `formatDuration` rules:
 * `<1s → "850ms"`, `<10s → "1.2s"`, otherwise whole seconds `"12s"`.
 * Language-independent by design (only the surrounding labels are i18n).
 * @param durationMs Milliseconds, or null/undefined/negative when unknown.
 * @returns The formatted duration, or '' when unknown (callers render nothing).
 */
export function formatToolDuration(durationMs: number | null | undefined): string {
  if (durationMs == null || Number.isNaN(Number(durationMs)) || durationMs < 0) return '';
  const ms = Math.round(Number(durationMs));
  if (ms < 1000) return `${ms}ms`;
  if (ms < 10000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 1000)}s`;
}

export const formatCompactTimeString = (timeStr: string | number, format: string = 'YYYY-MM-DD HH:mm'): string => {
  if (!timeStr) return '';

  // Normalize to a string and trim leading/trailing whitespace
  const str = String(timeStr).trim();

  // Strict validation: only parse when it is a standard 14-digit, all-numeric string
  // (the length can be fine-tuned to match what the backend actually returns)
  if (str.length !== 14 || isNaN(Number(str))) {
    return '';
  }

  // Core: pass the second argument 'YYYYMMDDHHmmss' to explicitly tell dayjs how to
  // break this string apart
  const date = dayjs(str, 'YYYYMMDDHHmmss');

  // Defensive: check whether the parsed date is valid
  return date.isValid() ? date.format(format) : '';
};

/** Session title length cap (counted in Unicode code points; CJK and Latin each count as 1) */
export const SESSION_TITLE_MAX_LENGTH = 30;

/**
 * Session title validity: ≤30 characters, allowing only letters (any language,
 * including Chinese/Japanese/Korean), digits, whitespace, and the three safe symbols
 * `.` `_` `-` (allowlist-based).
 * Blocks `< > " ' & ; / \` and other characters usable for XSS/injection at the input
 * side — the render layer's `{{ }}` interpolation escapes on its own, and this check
 * serves as defense in depth, guaranteeing titles never carry a payload in any
 * scenario (including possible future v-html / native DOM concatenation).
 */
const SESSION_TITLE_RE = new RegExp(`^[\\p{L}\\p{N}\\s._-]{1,${SESSION_TITLE_MAX_LENGTH}}$`, 'u');

export function isValidSessionTitle(title: string): boolean {
  return SESSION_TITLE_RE.test(title.trim());
}
