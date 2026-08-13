import { describe, it, expect, vi, afterEach } from 'vitest';
import type { Response } from '@/types/response';

// `fetchApi` is used inside workspace.ts as a Nuxt auto-import (no explicit
// `import` statement), so it must be stubbed as a global.
import {
  read_system_prompt_handler,
  write_system_prompt_file_handler,
  update_system_prompt_file_handler,
  read_character_handler,
  write_character_handler,
  update_character_handler,
} from '../workspace';

function stubFetchApi(data: unknown) {
  const mock = vi.fn().mockResolvedValue(data);
  vi.stubGlobal('fetchApi', mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('read_system_prompt_handler', () => {
  it('returns res.data as a Record', async () => {
    const data = { 'IDENTITY.md': 'Sherry', 'SOUL.md': 'detective' };
    const mock = stubFetchApi({ code: 200, data });
    await expect(read_system_prompt_handler()).resolves.toEqual(data);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: {},
      method: 'get',
    });
  });

  it('returns {} when data is falsy', async () => {
    stubFetchApi({ code: 200, data: null });
    await expect(read_system_prompt_handler()).resolves.toEqual({});
  });

  it('returns {} when fetchApi rejects', async () => {
    stubFetchApi(Promise.reject(new Error('boom')));
    await expect(read_system_prompt_handler()).resolves.toEqual({});
  });
});

describe('write_system_prompt_file_handler', () => {
  it('returns true on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(
      write_system_prompt_file_handler({ 'IDENTITY.md': 'X' }),
    ).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'IDENTITY.md': 'X' } },
      method: 'post',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(
      write_system_prompt_file_handler({ a: 'b' }),
    ).resolves.toBe(false);
  });
});

describe('update_system_prompt_file_handler', () => {
  it('returns true on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(
      update_system_prompt_file_handler({ 'SOUL.md': 'Y' }),
    ).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'SOUL.md': 'Y' } },
      method: 'patch',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(update_system_prompt_file_handler({ a: 'b' })).resolves.toBe(false);
  });
});

describe('read_character_handler', () => {
  it('returns true on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(
      read_character_handler({ name: { value: 'Sherry' } }),
    ).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/character',
      opts: { character_data: { name: { value: 'Sherry' } } },
      method: 'get',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(read_character_handler({ a: { b: 'c' } })).resolves.toBe(false);
  });
});

describe('write_character_handler', () => {
  it('sends a PUT request and returns true', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(
      write_character_handler({ name: { value: 'Sherry' } }),
    ).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/character',
      opts: { character_data: { name: { value: 'Sherry' } } },
      method: 'put',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(write_character_handler({ a: { b: 'c' } })).resolves.toBe(false);
  });
});

describe('update_character_handler', () => {
  it('sends a PATCH request and returns true', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(
      update_character_handler({ trait: { value: 'cold' } }),
    ).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/character',
      opts: { character_data: { trait: { value: 'cold' } } },
      method: 'patch',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(update_character_handler({ a: { b: 'c' } })).resolves.toBe(false);
  });
});
