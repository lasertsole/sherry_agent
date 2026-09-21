import { describe, it, expect } from 'vitest';
import { TODO_STATUS_ICON_FALLBACK, resolveTodoStatusIcon } from '../todo-status';

describe('resolveTodoStatusIcon', () => {
  it.each([
    ['completed', 'pi pi-check-circle text-green-500'],
    ['cancelled', 'pi pi-times-circle text-color-secondary'],
    ['in_progress', 'pi pi-spin pi-spinner text-primary']
  ])('status %s -> %s', (status, expected) => {
    expect(resolveTodoStatusIcon(status)).toBe(expected);
  });

  it('falls back to the circle icon for pending / unknown / empty statuses', () => {
    expect(resolveTodoStatusIcon('pending')).toBe(TODO_STATUS_ICON_FALLBACK);
    expect(resolveTodoStatusIcon('weird')).toBe(TODO_STATUS_ICON_FALLBACK);
    expect(resolveTodoStatusIcon(undefined)).toBe(TODO_STATUS_ICON_FALLBACK);
    expect(resolveTodoStatusIcon(null)).toBe(TODO_STATUS_ICON_FALLBACK);
    expect(resolveTodoStatusIcon('')).toBe(TODO_STATUS_ICON_FALLBACK);
    expect(TODO_STATUS_ICON_FALLBACK).toBe('pi pi-circle text-color-secondary');
  });
});
