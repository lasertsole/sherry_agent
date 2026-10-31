/**
 * App-level flags: small persisted switches that survive a reload (the
 * onboarding "seen" bit is the first one).
 *
 * Two stores on purpose. IndexedDB (Dexie `appFlags`) is the durable record —
 * it is the one the plan chose over localStorage, because clearing the browser
 * cache is a deliberate act rather than an accident. localStorage is a MIRROR
 * used as the FIRST read: when IndexedDB is unavailable (a hardened browser, a
 * private mode that blocks it, a broken profile) the flag still resolves, so a
 * first-run flow cannot resurface on every visit because storage failed. A
 * failed write to either store never throws — losing a flag is a UX nuisance,
 * not a crash.
 *
 * @module composables/use-app-flags
 */

/** localStorage mirror prefix (one key per flag). */
const MIRROR_PREFIX = 'appFlag:';

/**
 * The mirror key of one flag.
 * @param key Flag name.
 */
function mirrorKey(key: string): string {
  return `${MIRROR_PREFIX}${key}`;
}

/**
 * Read one flag: the localStorage mirror first (synchronous, independent of
 * IndexedDB health), then the Dexie row.
 * @param key Flag name.
 * @returns The stored value, or undefined when it was never written.
 */
export async function getAppFlag<T = unknown>(key: string): Promise<T | undefined> {
  if (typeof window !== 'undefined') {
    try {
      const raw = window.localStorage.getItem(mirrorKey(key));
      if (raw !== null) return JSON.parse(raw) as T;
    } catch {
      // A blocked / full localStorage falls through to the Dexie row.
    }
  }
  try {
    const value = await readAppFlag(key);
    return value as T | undefined;
  } catch {
    return undefined;
  }
}

/**
 * Write one flag to BOTH stores (each failure is swallowed independently).
 * @param key Flag name.
 * @param value Any JSON-serializable value.
 */
export async function setAppFlag(key: string, value: unknown): Promise<void> {
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(mirrorKey(key), JSON.stringify(value));
    } catch {
      // Quota / privacy mode: the Dexie row below is still attempted.
    }
  }
  try {
    await writeAppFlag(key, value);
  } catch {
    // IndexedDB unavailable: the mirror already carries the flag.
  }
}

/**
 * The flag controller components share (one shape, no per-call imports).
 */
export function useAppFlags() {
  return { getFlag: getAppFlag, setFlag: setAppFlag };
}
