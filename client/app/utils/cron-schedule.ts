/**
 * Cron schedule form <-> wire conversions (CronDialog).
 *
 * Extracted from `CronDialog.vue` so the three previously switch-driven mappings
 * are table-driven and unit-testable. Every branch keeps its exact former output
 * (including the `default` fallbacks), pinned by `utils/__tests__/cron-schedule.test.ts`.
 *
 * @module utils/cron-schedule
 */
import type { CronSchedule } from '@/composables/bridge';

/** Schedule kinds supported by the dialog. */
export type ScheduleType = 'at' | 'every' | 'cron';

/** Translator signature used by the schedule describers. */
export type ScheduleTranslator = (key: string, named: Record<string, unknown>) => string;

export const DAY_MS = 24 * 60 * 60 * 1000;
export const HOUR_MS = 60 * 60 * 1000;
export const MINUTE_MS = 60 * 1000;
export const SECOND_MS = 1000;

/** Millis per interval unit (the former `everyToMs` switch). */
export const EVERY_UNIT_MS: Record<string, number> = {
  s: SECOND_MS,
  m: MINUTE_MS,
  h: HOUR_MS,
  d: DAY_MS
};

/**
 * Convert an interval value + unit into milliseconds.
 * Returns null for a non-positive value or an unknown unit (former `default`).
 * @param value
 * @param unit
 */
export function everyValueToMs(value: number | null | undefined, unit: string): number | null {
  if (!value || value <= 0) return null;
  const factor = EVERY_UNIT_MS[unit];
  return factor === undefined ? null : value * factor;
}

/** The schedule-relevant slice of the dialog form. */
export interface ScheduleForm {
  scheduleType: ScheduleType;
  atDate: Date | null;
  everyValue: number | null;
  everyUnit: string;
  expr: string;
}

/** Per-kind schedule builders (the former `buildSchedule` switch). */
const SCHEDULE_BUILDERS: Record<ScheduleType, (form: ScheduleForm) => CronSchedule> = {
  at: form => ({ kind: 'at', atMs: form.atDate ? form.atDate.getTime() : null }),
  every: form => ({ kind: 'every', everyMs: everyValueToMs(form.everyValue, form.everyUnit) }),
  cron: form => ({ kind: 'cron', expr: form.expr.trim() })
};

/**
 * Build the wire schedule from the form (unknown type falls back to `every`).
 * @param form
 */
export function buildSchedule(form: ScheduleForm): CronSchedule {
  const builder = SCHEDULE_BUILDERS[form.scheduleType] ?? SCHEDULE_BUILDERS.every;
  return builder(form);
}

/**
 * Human-readable interval (former `fmtInterval`): largest whole unit first.
 * @param ms
 * @param t
 */
export function fmtInterval(ms: number | null | undefined, t: ScheduleTranslator): string {
  if (!ms) return '';
  if (ms % DAY_MS === 0) return t('config.cron.everyDays', { n: ms / DAY_MS });
  if (ms % HOUR_MS === 0) return t('config.cron.everyHours', { n: ms / HOUR_MS });
  if (ms % MINUTE_MS === 0) return t('config.cron.everyMinutes', { n: ms / MINUTE_MS });
  return t('config.cron.everySeconds', { n: ms / SECOND_MS });
}

/**
 * Describe a stored schedule (former `describeSchedule` switch; unknown kind
 * falls back to the cron branch).
 * @param schedule
 * @param t
 * @param formatTime
 */
export function describeSchedule(
  schedule: CronSchedule,
  t: ScheduleTranslator,
  formatTime: (ms: number) => string
): string {
  const describers: Record<ScheduleType, () => string> = {
    at: () =>
      schedule.atMs ? t('config.cron.descAt', { time: formatTime(schedule.atMs) }) : t('config.cron.descAtEmpty', {}),
    every: () => fmtInterval(schedule.everyMs, t),
    cron: () => schedule.expr ?? ''
  };
  const describer = describers[schedule.kind] ?? describers.cron;
  return describer();
}
