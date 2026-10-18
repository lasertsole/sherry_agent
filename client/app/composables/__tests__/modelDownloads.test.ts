import { describe, it, expect, vi, beforeEach } from 'vitest';

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import { fetchModelDownloadsStatus, startModelDownload } from '../model-downloads';

beforeEach(() => {
  fetchApiMock.mockReset();
  fetchApiMock.mockResolvedValue({ models: {} });
});

describe('local model download bridge', () => {
  it('reads GET /model-downloads/status', async () => {
    await fetchModelDownloadsStatus();
    expect(fetchApiMock).toHaveBeenCalledWith({
      url: '/model-downloads/status',
      opts: { _ts: expect.any(Number) },
      method: 'get'
    });
  });

  it('starts POST /model-downloads/download for a family', async () => {
    await startModelDownload('EMBEDDING');
    expect(fetchApiMock).toHaveBeenCalledWith({
      url: '/model-downloads/download',
      opts: { family: 'EMBEDDING' },
      method: 'post'
    });
  });
});
