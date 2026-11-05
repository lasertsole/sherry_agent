/**
 * `describeError`: the Tauri rejection shape must never render "[object Object]".
 *
 * The Rust shell serializes `FrontendError { code, message }` as a PLAIN object,
 * so `e instanceof Error ? e.message : String(e)` produced the useless
 * "( [object Object] )" suffix the first-run dialogs showed.
 */
import { describe, expect, it } from 'vitest';
import { describeError } from '@/utils/describe-error';

describe('describeError', () => {
  it('returns an Error message', () => {
    expect(describeError(new Error('boom'))).toBe('boom');
  });

  it('prefers a Tauri error object message and keeps its code', () => {
    expect(describeError({ code: 'BACKEND_ERROR', message: 'HTTP 500: nope' })).toBe(
      'BACKEND_ERROR: HTTP 500: nope'
    );
  });

  it('renders a message without a code verbatim', () => {
    expect(describeError({ message: 'Permission denied' })).toBe('Permission denied');
  });

  it('JSON-dumps an object without a message instead of String()', () => {
    expect(describeError({ status: 500 })).toBe('{"status":500}');
  });

  it('strings a primitive', () => {
    expect(describeError('plain')).toBe('plain');
    expect(describeError(42)).toBe('42');
  });

  it('renders an empty string for null / undefined', () => {
    expect(describeError(null)).toBe('');
    expect(describeError(undefined)).toBe('');
  });
});
