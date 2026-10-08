import { describe, it, expect, vi, afterEach } from 'vitest';

// `fetchApi` is used inside workspace.ts as a Nuxt auto-import: the binding
// resolves through the `../requestApi` module (unimport injection, see
// vitest.config.ts), so mock that module — a globalThis stub would never be
// read by an import binding.
const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import {
  read_system_prompt_handler,
  write_system_prompt_file_handler,
  update_system_prompt_file_handler
} from '../workspace';

function stubFetchApi(data: unknown) {
  fetchApiMock.mockReset();
  fetchApiMock.mockResolvedValue(data);
  return fetchApiMock;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('read_system_prompt_handler', () => {
  it('returns res.data as a Record', async () => {
    const data = { 'SOUL.md': 'Sherry', 'USER.md': 'detective' };
    const mock = stubFetchApi({ code: 200, data });
    await expect(read_system_prompt_handler()).resolves.toEqual(data);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: {},
      method: 'get'
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
    await expect(write_system_prompt_file_handler({ 'SOUL.md': 'X' })).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'SOUL.md': 'X' } },
      method: 'post'
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(write_system_prompt_file_handler({ a: 'b' })).resolves.toBe(false);
  });
});

describe('update_system_prompt_file_handler', () => {
  it('returns true on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(update_system_prompt_file_handler({ 'SOUL.md': 'Y' })).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'SOUL.md': 'Y' } },
      method: 'patch'
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(update_system_prompt_file_handler({ a: 'b' })).resolves.toBe(false);
  });
});
