import { describe, it, expect, vi, beforeEach } from 'vitest';

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import { disableCodeEmbedder, fetchCodeEmbedderStatus, startCodeEmbedderDownload } from '../code-embedder';

beforeEach(() => {
  fetchApiMock.mockReset();
  fetchApiMock.mockResolvedValue({ state: 'idle' });
});

describe('code embedder bridge', () => {
  it('reads GET /code-embedder/status', async () => {
    await fetchCodeEmbedderStatus();
    expect(fetchApiMock).toHaveBeenCalledWith({
      url: '/code-embedder/status',
      opts: { _ts: expect.any(Number) },
      method: 'get'
    });
  });

  it('starts POST /code-embedder/download', async () => {
    await startCodeEmbedderDownload();
    expect(fetchApiMock).toHaveBeenCalledWith({
      url: '/code-embedder/download',
      opts: {},
      method: 'post'
    });
  });

  it('disables via DELETE /code-embedder', async () => {
    await disableCodeEmbedder();
    expect(fetchApiMock).toHaveBeenCalledWith({
      url: '/code-embedder',
      opts: {},
      method: 'delete'
    });
  });
});
