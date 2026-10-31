/**
 * App flags: the persistent "seen" bits that keep a first-run flow from
 * reappearing. Dexie is the durable record, localStorage the first-read mirror
 * that survives a blocked IndexedDB.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const db = vi.hoisted(() => ({
  readAppFlag: vi.fn(),
  writeAppFlag: vi.fn(async () => undefined)
}));
vi.mock('@/composables/db', () => ({
  readAppFlag: db.readAppFlag,
  writeAppFlag: db.writeAppFlag
}));

import { getAppFlag, setAppFlag, useAppFlags } from '../use-app-flags';

beforeEach(() => {
  window.localStorage.clear();
  db.readAppFlag.mockReset().mockResolvedValue(undefined);
  db.writeAppFlag.mockReset().mockResolvedValue(undefined);
});

describe('setAppFlag', () => {
  it('writes BOTH the localStorage mirror and the Dexie row', async () => {
    await setAppFlag('onboarding_completed', true);

    expect(window.localStorage.getItem('appFlag:onboarding_completed')).toBe('true');
    expect(db.writeAppFlag).toHaveBeenCalledWith('onboarding_completed', true);
  });

  it('does not reject when IndexedDB refuses the write', async () => {
    db.writeAppFlag.mockRejectedValue(new Error('indexeddb blocked'));

    await expect(setAppFlag('onboarding_completed', true)).resolves.toBeUndefined();
    expect(window.localStorage.getItem('appFlag:onboarding_completed')).toBe('true');
  });
});

describe('getAppFlag', () => {
  it('reads the localStorage mirror without touching Dexie', async () => {
    window.localStorage.setItem('appFlag:onboarding_completed', 'true');

    expect(await getAppFlag<boolean>('onboarding_completed')).toBe(true);
    expect(db.readAppFlag).not.toHaveBeenCalled();
  });

  it('falls back to the Dexie row when the mirror is absent', async () => {
    db.readAppFlag.mockResolvedValue(true);

    expect(await getAppFlag<boolean>('onboarding_completed')).toBe(true);
  });

  it('reads as undefined when both stores are empty', async () => {
    expect(await getAppFlag<boolean>('onboarding_completed')).toBeUndefined();
  });

  it('reads as undefined, never throws, when Dexie fails', async () => {
    db.readAppFlag.mockRejectedValue(new Error('closed'));

    await expect(getAppFlag('onboarding_completed')).resolves.toBeUndefined();
  });

  it('prefers the mirror so a blocked IndexedDB cannot resurrect the tour', async () => {
    window.localStorage.setItem('appFlag:onboarding_completed', 'true');
    db.readAppFlag.mockRejectedValue(new Error('blocked'));

    expect(await getAppFlag<boolean>('onboarding_completed')).toBe(true);
  });
});

describe('useAppFlags', () => {
  it('exposes the same pair the components use', async () => {
    const flags = useAppFlags();
    await flags.setFlag('k', 'v');

    expect(await flags.getFlag<string>('k')).toBe('v');
  });
});
