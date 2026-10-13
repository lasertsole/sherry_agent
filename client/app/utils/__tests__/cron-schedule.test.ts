import { describe, it, expect } from 'vitest';
import {
  DAY_MS,
  HOUR_MS,
  MINUTE_MS,
  SECOND_MS,
  everyValueToMs,
  buildSchedule,
  describeSchedule,
  fmtInterval,
  type ScheduleForm,
  type ScheduleType,
  type ScheduleTranslator
} from '../cron-schedule';
import type { CronSchedule } from '@/composables/bridge';

/**
 * Stub translator: renders `key|named-json` so every named argument is pinned.
 * @param key
 * @param named
 */
const stubT: ScheduleTranslator = (key, named) => `${key}|${JSON.stringify(named)}`;
const stubFormatTime = (ms: number) => `T${ms}`;

const asScheduleType = (value: unknown): ScheduleType => value as ScheduleType;
const asSchedule = (value: unknown): CronSchedule => value as CronSchedule;

const form = (over: Partial<ScheduleForm>): ScheduleForm => ({
  scheduleType: 'every',
  atDate: null,
  everyValue: 5,
  everyUnit: 'm',
  expr: '',
  ...over
});

describe('everyValueToMs', () => {
  it.each([
    ['s', 5, 5 * SECOND_MS],
    ['m', 5, 5 * MINUTE_MS],
    ['h', 2, 2 * HOUR_MS],
    ['d', 3, 3 * DAY_MS]
  ])('unit %s value %d -> %d ms', (unit, value, expected) => {
    expect(everyValueToMs(value, unit)).toBe(expected);
  });

  it('returns null for a zero/negative/absent value or an unknown unit (default branch)', () => {
    expect(everyValueToMs(0, 'm')).toBeNull();
    expect(everyValueToMs(-1, 'm')).toBeNull();
    expect(everyValueToMs(null, 'm')).toBeNull();
    expect(everyValueToMs(undefined, 'm')).toBeNull();
    expect(everyValueToMs(5, 'x')).toBeNull();
  });
});

describe('buildSchedule', () => {
  it('at: carries the epoch millis, or null when the date is unset', () => {
    expect(buildSchedule(form({ scheduleType: 'at', atDate: new Date(1234567890) }))).toEqual({
      kind: 'at',
      atMs: 1234567890
    });
    expect(buildSchedule(form({ scheduleType: 'at', atDate: null }))).toEqual({ kind: 'at', atMs: null });
  });

  it('every: converts the value+unit (null for an invalid value)', () => {
    expect(buildSchedule(form({ scheduleType: 'every', everyValue: 5, everyUnit: 'm' }))).toEqual({
      kind: 'every',
      everyMs: 300000
    });
    expect(buildSchedule(form({ scheduleType: 'every', everyValue: null, everyUnit: 'm' }))).toEqual({
      kind: 'every',
      everyMs: null
    });
  });

  it('cron: trims the expression', () => {
    expect(buildSchedule(form({ scheduleType: 'cron', expr: ' 0 9 * * * ' }))).toEqual({
      kind: 'cron',
      expr: '0 9 * * *'
    });
  });

  it('an unknown schedule type falls back to the every branch (former default)', () => {
    const result = buildSchedule(form({ scheduleType: asScheduleType('weird'), everyValue: 1, everyUnit: 's' }));
    expect(result).toEqual({ kind: 'every', everyMs: 1000 });
  });
});

describe('fmtInterval', () => {
  it('picks the largest whole unit in the former precedence order', () => {
    expect(fmtInterval(2 * DAY_MS, stubT)).toBe('config.cron.everyDays|{"n":2}');
    expect(fmtInterval(3 * HOUR_MS, stubT)).toBe('config.cron.everyHours|{"n":3}');
    expect(fmtInterval(90 * MINUTE_MS, stubT)).toBe('config.cron.everyMinutes|{"n":90}');
    expect(fmtInterval(1500, stubT)).toBe('config.cron.everySeconds|{"n":1.5}');
  });

  it('returns an empty string for a falsy interval', () => {
    expect(fmtInterval(null, stubT)).toBe('');
    expect(fmtInterval(0, stubT)).toBe('');
    expect(fmtInterval(undefined, stubT)).toBe('');
  });
});

describe('describeSchedule', () => {
  it('at: formats the time, or shows the empty label without a timestamp', () => {
    expect(describeSchedule({ kind: 'at', atMs: 123 }, stubT, stubFormatTime)).toBe(
      'config.cron.descAt|{"time":"T123"}'
    );
    expect(describeSchedule({ kind: 'at', atMs: null }, stubT, stubFormatTime)).toBe('config.cron.descAtEmpty|{}');
  });

  it('every: delegates to the interval formatter (empty for a missing interval)', () => {
    expect(describeSchedule({ kind: 'every', everyMs: 2 * DAY_MS }, stubT, stubFormatTime)).toBe(
      'config.cron.everyDays|{"n":2}'
    );
    expect(describeSchedule({ kind: 'every', everyMs: null }, stubT, stubFormatTime)).toBe('');
  });

  it('cron: returns the raw expression (empty when absent)', () => {
    expect(describeSchedule({ kind: 'cron', expr: '0 9 * * *' }, stubT, stubFormatTime)).toBe('0 9 * * *');
    expect(describeSchedule({ kind: 'cron', expr: null }, stubT, stubFormatTime)).toBe('');
  });

  it('an unknown kind falls back to the cron branch (former default)', () => {
    expect(describeSchedule(asSchedule({ kind: 'weird', expr: 'raw' }), stubT, stubFormatTime)).toBe('raw');
  });
});
