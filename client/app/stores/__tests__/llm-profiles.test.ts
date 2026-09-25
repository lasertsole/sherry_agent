import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useLlmProfilesStore } from '../llm-profiles';

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
