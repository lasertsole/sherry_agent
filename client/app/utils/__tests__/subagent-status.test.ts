import { describe, it, expect } from 'vitest';
import {
  SUBAGENT_STATUS_FALLBACK,
  SUBAGENT_STATUS_META,
  subagentStatusColorDark,
  subagentStatusColorLight,
  subagentStatusMeta
} from '../subagent-status';

/**
 * These are frozen presentation values: the i18n keys and Tailwind class
 * strings below were previously duplicated across four call sites. Any change
 * here is a user-visible change and must be deliberate.
 */
describe('SUBAGENT_STATUS_META', () => {
  it('pins every status value byte-for-byte', () => {
    expect(SUBAGENT_STATUS_META).toEqual({
      RUNNING: {
        labelKey: 'sidebar.statusRunning',
        badgeClass: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
        colorLight: '#3b82f6',
        colorDark: '#60a5fa',
        flowKey: 'running'
      },
      INTERRUPTED: {
        labelKey: 'sidebar.statusInterrupted',
        badgeClass: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
        colorLight: '#3b82f6',
        colorDark: '#60a5fa',
        flowKey: 'running'
      },
      PENDING: {
        labelKey: 'sidebar.statusPending',
        badgeClass: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
        colorLight: '#64748b',
        colorDark: '#94a3b8',
        flowKey: 'unknown'
      },
      IN_PROGRESS: {
        labelKey: 'sidebar.statusInProgress',
        badgeClass: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
        colorLight: '#64748b',
        colorDark: '#94a3b8',
        flowKey: 'unknown'
      },
      DELIVERED: {
        labelKey: 'sidebar.statusDelivered',
        badgeClass: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
        colorLight: '#64748b',
        colorDark: '#94a3b8',
        flowKey: 'unknown'
      },
      OK: {
        labelKey: 'sidebar.statusDone',
        badgeClass: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
        colorLight: '#10b981',
        colorDark: '#34d399',
        flowKey: 'completed'
      },
      ERROR: {
        labelKey: 'sidebar.statusError',
        badgeClass: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
        colorLight: '#ef4444',
        colorDark: '#f87171',
        flowKey: 'failed'
      },
      TIMEOUT: {
        labelKey: 'sidebar.statusTimeout',
        badgeClass: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
        colorLight: '#ef4444',
        colorDark: '#f87171',
        flowKey: 'failed'
      },
      KILLED: {
        labelKey: 'sidebar.statusKilled',
        badgeClass: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
        colorLight: '#ef4444',
        colorDark: '#f87171',
        flowKey: 'failed'
      }
    });
  });

  it('pins the fallback entry', () => {
    expect(SUBAGENT_STATUS_FALLBACK).toEqual({
      labelKey: 'sidebar.statusUnknown',
      badgeClass: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
      colorLight: '#64748b',
      colorDark: '#94a3b8',
      flowKey: 'unknown'
    });
  });

  it('resolves known statuses to their own entry', () => {
    expect(subagentStatusMeta('RUNNING')).toBe(SUBAGENT_STATUS_META.RUNNING);
    expect(subagentStatusMeta('OK')).toBe(SUBAGENT_STATUS_META.OK);
    expect(subagentStatusMeta('DELIVERED')).toBe(SUBAGENT_STATUS_META.DELIVERED);
  });

  it.each([undefined, null, '', 'WEIRD'])('falls back for the unknown status %o', status => {
    expect(subagentStatusMeta(status)).toBe(SUBAGENT_STATUS_FALLBACK);
    expect(subagentStatusColorLight(status)).toBe('#64748b');
    expect(subagentStatusColorDark(status)).toBe('#94a3b8');
  });

  it('maps raw statuses to the frozen node colors', () => {
    expect(subagentStatusColorLight('RUNNING')).toBe('#3b82f6');
    expect(subagentStatusColorDark('RUNNING')).toBe('#60a5fa');
    expect(subagentStatusColorLight('OK')).toBe('#10b981');
    expect(subagentStatusColorDark('OK')).toBe('#34d399');
    expect(subagentStatusColorLight('ERROR')).toBe('#ef4444');
    expect(subagentStatusColorDark('TIMEOUT')).toBe('#f87171');
  });
});
