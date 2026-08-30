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
export const formatCompactTimeString = (timeStr: string | number, format: string = 'YYYY-MM-DD HH:ss'): string => {
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
