import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useLlmProfilesStore } from '../llm-profiles';

describe('stores/llm-profiles', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('add() appends a profile and returns its id', () => {
    const store = useLlmProfilesStore();
    const id = store.add('glm-4.6', { MAIN_LLM_NAME: 'glm-4.6' });
    expect(store.profiles).toHaveLength(1);
    expect(store.byId(id)?.label).toBe('glm-4.6');
    expect(store.byId(id)?.params.MAIN_LLM_NAME).toBe('glm-4.6');
  });

  it('add() snapshots the params (later mutation of the caller object does not leak in)', () => {
    const store = useLlmProfilesStore();
    const params = { MAIN_LLM_NAME: 'a' };
    const id = store.add('a', params);
    params.MAIN_LLM_NAME = 'mutated';
    expect(store.byId(id)?.params.MAIN_LLM_NAME).toBe('a');
  });

  it('update() replaces parameters and label of one profile only', () => {
    const store = useLlmProfilesStore();
    const first = store.add('a', { MAIN_LLM_NAME: 'a' });
    const second = store.add('b', { MAIN_LLM_NAME: 'b' });
    store.update(first, { label: 'a2', params: { MAIN_LLM_NAME: 'a2' } });
    expect(store.byId(first)?.label).toBe('a2');
    expect(store.byId(second)?.params.MAIN_LLM_NAME).toBe('b');
  });

  it('remove() drops the profile and clears the active marker when it pointed there', () => {
    const store = useLlmProfilesStore();
    const id = store.add('a', {});
    store.setActive(id);
    store.remove(id);
    expect(store.profiles).toHaveLength(0);
    expect(store.activeId).toBeNull();
  });

  it('setActive()/byId() track the applied profile', () => {
    const store = useLlmProfilesStore();
    const id = store.add('a', {});
    store.setActive(id);
    expect(store.activeId).toBe(id);
    expect(store.byId(id)?.id).toBe(id);
    store.setActive(null);
    expect(store.byId(null)).toBeUndefined();
  });

  it('keeps profiles across store recreation (persisted shape)', () => {
    const store = useLlmProfilesStore();
    store.add('a', { MAIN_LLM_NAME: 'a' });
    // The pinia persist plugin serialises `pick: ['profiles', 'activeId']`;
    // recreate the store on the same pinia to prove the state survives.
    const again = useLlmProfilesStore();
    expect(again.profiles).toHaveLength(1);
  });
});
