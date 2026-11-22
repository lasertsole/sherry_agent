/**
 * Expert registry store (S2). The backend API is not shipped yet, so the store
 * runs in its documented mock mode; pin the CRUD shape so switching to the real
 * `/api/experts` later is a transport change only.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createPinia } from 'pinia';
import { useExpertRegistryStore } from '../expert-registry';

describe('stores/expert-registry — mock CRUD', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('loads seeded experts in mock mode', async () => {
    const store = useExpertRegistryStore();
    await store.loadAll();
    expect(store.experts.length).toBeGreaterThan(0);
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });

  it('creates, updates and removes an expert', async () => {
    const store = useExpertRegistryStore();
    await store.loadAll();
    const config = store.newConfig();
    config.name = 'test-expert';
    config.description = 'temporary';
    await store.create(config);
    expect(store.byName.has('test-expert')).toBe(true);

    await store.update('test-expert', { description: 'updated' });
    expect(store.byName.get('test-expert')?.description).toBe('updated');

    await store.remove('test-expert');
    expect(store.byName.has('test-expert')).toBe(false);
  });

  it('idempotent create does not duplicate a name', async () => {
    const store = useExpertRegistryStore();
    await store.loadAll();
    const config = store.newConfig();
    config.name = 'frontend-expert';
    await store.create(config);
    expect(store.experts.filter(e => e.name === 'frontend-expert')).toHaveLength(1);
  });
});
