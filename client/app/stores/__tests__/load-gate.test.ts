/**
 * The load-gate notice store: a deferred dispatch becomes one Notifications
 * entry with localized numbers, and the listener is registered once.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';

const mitt = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mitt);

const i18nRuntime = vi.hoisted(() => ({ resolveRuntimeT: vi.fn() }));
vi.mock('@/composables/i18nRuntime', () => i18nRuntime);

const notifications = vi.hoisted(() => ({ handleNotification: vi.fn() }));

import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';

import { loadWarningText, useLoadGateStore } from '../load-gate';

/** The handler the store registered for the frame, once subscribed. */
function warningHandler(): (payload: unknown) => void {
  return mitt.on.mock.calls.find(call => call[0] === 'ws:load_warning')?.[1] as (payload: unknown) => void;
}

describe('stores/load-gate', () => {
  beforeEach(() => {
    // A fresh pinia per test: the store's `subscribed` guard lives in its
    // closure, so a shared instance would make the second subscribe() a no-op.
    setActivePinia(createTestingPinia({ stubActions: false }));
    mitt.on.mockClear();
    i18nRuntime.resolveRuntimeT.mockReset();
    notifications.handleNotification.mockReset();
    vi.stubGlobal(
      'useNotificationStore',
      vi.fn(() => notifications)
    );
  });

  it('subscribes once', () => {
    const store = useLoadGateStore();
    store.subscribe();
    store.subscribe();

    expect(mitt.on.mock.calls.filter(call => call[0] === 'ws:load_warning')).toHaveLength(1);
  });

  it('turns a warning frame into a localized notice', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string, params?: Record<string, unknown>) =>
      key === 'loadGate.warning' ? `主机负载过高 ${params?.cpu}/${params?.ram}` : key
    );
    const store = useLoadGateStore();
    store.subscribe();

    warningHandler()({
      event: 'load_warning',
      session_id: 'sid-1',
      content: { cpu: 97.5, ram: 96, run_id: 'run-1' }
    });

    expect(notifications.handleNotification).toHaveBeenCalledWith('主机负载过高 97.5/96', 'load_gate');
    expect(store.lastWarning).toEqual({ cpu: 97.5, ram: 96, runId: 'run-1' });
  });

  it('ignores a frame without a session id', () => {
    const store = useLoadGateStore();

    store.handleLoadWarning({ content: { cpu: 99, ram: 99, run_id: 'x' } });

    expect(notifications.handleNotification).not.toHaveBeenCalled();
  });

  it('falls back to raw numbers without i18n', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue(undefined);

    expect(loadWarningText(97, 96)).toContain('97');
    expect(loadWarningText(97, 96)).toContain('96');
  });
});
