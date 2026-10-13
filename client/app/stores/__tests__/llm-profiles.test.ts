import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { MAX_PROFILES_PER_GROUP, useLlmProfilesStore } from '../llm-profiles';

describe('stores/llm-profiles (per group)', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('add() appends to its group and returns the id', () => {
    const store = useLlmProfilesStore();
    const id = store.add('MAIN_LLM', 'glm-4.6', { MAIN_LLM_NAME: 'glm-4.6' });
    expect(store.listFor('MAIN_LLM')).toHaveLength(1);
    expect(store.byId('MAIN_LLM', id)?.label).toBe('glm-4.6');
    expect(store.byId('MAIN_LLM', id)?.params.MAIN_LLM_NAME).toBe('glm-4.6');
  });

  it('keeps groups fully isolated', () => {
    const store = useLlmProfilesStore();
    store.add('MAIN_LLM', 'main', { MAIN_LLM_NAME: 'main' });
    store.add('TTI', 'tti-a', { TTI_API_NAME: 'tti-a' });
    store.add('TTI', 'tti-b', { TTI_API_NAME: 'tti-b' });
    expect(store.listFor('MAIN_LLM')).toHaveLength(1);
    expect(store.listFor('TTI')).toHaveLength(2);
    expect(store.listFor('STT')).toHaveLength(0);
  });

  it('add() snapshots params and update() patches one profile of one group', () => {
    const store = useLlmProfilesStore();
    const params = { TTI_API_NAME: 'a' };
    const id = store.add('TTI', 'a', params);
    params.TTI_API_NAME = 'mutated';
    expect(store.byId('TTI', id)?.params.TTI_API_NAME).toBe('a');
    store.update('TTI', id, { label: 'a2', params: { TTI_API_NAME: 'a2' } });
    expect(store.byId('TTI', id)?.label).toBe('a2');
    expect(store.byId('TTI', id)?.params.TTI_API_NAME).toBe('a2');
  });

  it('refuses to add beyond the per-group cap', () => {
    const store = useLlmProfilesStore();
    for (let i = 0; i < MAX_PROFILES_PER_GROUP; i++) {
      expect(store.add('TTI', `m${i}`, {})).not.toBeNull();
    }
    expect(store.add('TTI', 'overflow', {})).toBeNull();
    expect(store.listFor('TTI')).toHaveLength(MAX_PROFILES_PER_GROUP);
  });

  it('trims oversized storage by creation time, dropping the NEWEST entries', () => {
    const store = useLlmProfilesStore();
    // Oversized payload as an older/hand-edited localStorage would carry it
    // (the add() guard refuses to create one), with explicit creation stamps.
    const total = MAX_PROFILES_PER_GROUP + 3;
    store.byGroup = {
      TTI: Array.from({ length: total }, (_, i) => ({
        id: `p${i}`,
        label: `m${i}`,
        params: {},
        createdAt: 1000 + i
      }))
    };

    const dropped = store.trimGroup('TTI');

    expect(dropped).toBe(total - MAX_PROFILES_PER_GROUP);
    const kept = store.listFor('TTI');
    expect(kept).toHaveLength(MAX_PROFILES_PER_GROUP);
    // the oldest survive, the newest overflow is gone
    expect(kept[0]!.label).toBe('m0');
    expect(kept[kept.length - 1]!.label).toBe(`m${MAX_PROFILES_PER_GROUP - 1}`);
  });

  it('orders legacy entries without createdAt by their array index', () => {
    const store = useLlmProfilesStore();
    const total = MAX_PROFILES_PER_GROUP + 2;
    store.byGroup = {
      TTI: Array.from({ length: total }, (_, i) => ({ id: `p${i}`, label: `m${i}`, params: {} }))
    };
    expect(store.trimGroup('TTI')).toBe(2);
    const kept = store.listFor('TTI');
    expect(kept[0]!.label).toBe('m0');
    expect(kept[kept.length - 1]!.label).toBe(`m${MAX_PROFILES_PER_GROUP - 1}`);
  });

  it('keeps the active marker valid when its profile is trimmed away', () => {
    const store = useLlmProfilesStore();
    const total = MAX_PROFILES_PER_GROUP + 1;
    store.byGroup = {
      TTI: Array.from({ length: total }, (_, i) => ({
        id: `p${i}`,
        label: `m${i}`,
        params: {},
        createdAt: 1000 + i
      }))
    };
    const newest = store.listFor('TTI')[total - 1]!;
    store.setActive('TTI', newest.id);
    store.trimGroup('TTI');
    expect(store.listFor('TTI').some(p => p.id === newest.id)).toBe(false);
    // marker falls back to the surviving tail instead of dangling
    expect(store.activeIdFor('TTI')).toBe(store.listFor('TTI')[MAX_PROFILES_PER_GROUP - 1]!.id);
  });

  it('tracks the active marker per group and clears it on removal', () => {
    const store = useLlmProfilesStore();
    const mainId = store.add('MAIN_LLM', 'm', {});
    const ttiId = store.add('TTI', 't', {});
    store.setActive('MAIN_LLM', mainId);
    store.setActive('TTI', ttiId);
    expect(store.activeIdFor('MAIN_LLM')).toBe(mainId);
    expect(store.activeIdFor('TTI')).toBe(ttiId);
    store.remove('MAIN_LLM', mainId);
    expect(store.activeIdFor('MAIN_LLM')).toBeNull();
    expect(store.activeIdFor('TTI')).toBe(ttiId);
  });
});
