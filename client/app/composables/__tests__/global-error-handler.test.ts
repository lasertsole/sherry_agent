import { describe, it, expect, vi, afterEach } from 'vitest';
import { logUtil } from '~/utils/log';
import { installGlobalErrorHandler } from '../global-error-handler';

// Note: the module keeps a module-level `activeCleanup`; every test uninstalls
// through the returned handle (or the extra `installGlobalErrorHandler(window)`
// cleanup) so cases stay isolated.

afterEach(() => {
  vi.restoreAllMocks();
});

describe('installGlobalErrorHandler', () => {
  it('routes uncaught window errors into logUtil', () => {
    const spy = vi.spyOn(logUtil, 'e').mockImplementation(() => {});
    const cleanup = installGlobalErrorHandler(window);

    window.dispatchEvent(
      new ErrorEvent('error', {
        message: 'boom',
        error: new Error('boom'),
        filename: 'app.js',
        lineno: 3,
        colno: 9
      })
    );

    expect(spy).toHaveBeenCalledTimes(1);
    // First arg is the stable message, second carries the error detail.
    expect(spy.mock.calls[0]?.[0]).toContain('[global] Uncaught error');
    expect(String(spy.mock.calls[0]?.[1])).toContain('boom');

    cleanup();
  });

  it('falls back to the event message when no Error object is attached', () => {
    const spy = vi.spyOn(logUtil, 'e').mockImplementation(() => {});
    const cleanup = installGlobalErrorHandler(window);

    window.dispatchEvent(new ErrorEvent('error', { message: 'plain failure' }));

    expect(spy).toHaveBeenCalledTimes(1);
    expect(String(spy.mock.calls[0]?.[1])).toContain('plain failure');

    cleanup();
  });

  it('routes unhandled promise rejections (non-Error reasons included) into logUtil', () => {
    const spy = vi.spyOn(logUtil, 'e').mockImplementation(() => {});
    const cleanup = installGlobalErrorHandler(window);

    const rejection = new Event('unhandledrejection') as Event & { reason?: unknown };
    Object.defineProperty(rejection, 'reason', { value: { code: 42 } });
    window.dispatchEvent(rejection);

    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0]?.[0]).toContain('Unhandled promise rejection');
    expect(String(spy.mock.calls[0]?.[1])).toContain('42');

    cleanup();
  });

  it('removes both listeners on cleanup', () => {
    const spy = vi.spyOn(logUtil, 'e').mockImplementation(() => {});
    const cleanup = installGlobalErrorHandler(window);
    cleanup();

    window.dispatchEvent(new ErrorEvent('error', { message: 'after cleanup' }));
    const rejection = new Event('unhandledrejection') as Event & { reason?: unknown };
    Object.defineProperty(rejection, 'reason', { value: new Error('after cleanup') });
    window.dispatchEvent(rejection);

    expect(spy).not.toHaveBeenCalled();
  });

  it('is idempotent: duplicate installs do not stack listeners', () => {
    const spy = vi.spyOn(logUtil, 'e').mockImplementation(() => {});
    const first = installGlobalErrorHandler(window);
    const second = installGlobalErrorHandler(window);

    window.dispatchEvent(new ErrorEvent('error', { message: 'once' }));
    expect(spy).toHaveBeenCalledTimes(1);

    // The handle returned by the duplicate install is the same cleanup.
    second();
    window.dispatchEvent(new ErrorEvent('error', { message: 'twice' }));
    expect(spy).toHaveBeenCalledTimes(1);

    first();
  });
});
