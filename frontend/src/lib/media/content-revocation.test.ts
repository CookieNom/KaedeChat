import { afterEach, describe, expect, it, vi } from 'vitest';
import { authenticatedMediaBlob, saveMediaBlob } from './authenticated';
import {
  createMediaObjectURL,
  mediaReference,
  releaseMedia,
  revokeMedia,
  trackMediaBlob
} from './content-revocation';

afterEach(() => vi.unstubAllGlobals());

describe('revoked media', () => {
  it('recognizes regular and signed history media references', () => {
    expect(mediaReference('/api/v1/media/home.example/7001/thumbnail_512')).toBe(
      '7001@home.example'
    );
    expect(
      mediaReference(
        '/api/v1/dms/4%40home.example/history-media/5%40remote.example/7001%40home.example/original?token=opaque'
      )
    ).toBe('7001@home.example');
  });

  it('revokes existing object URLs and prevents later downloads of held blobs', async () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:revoked');
    const blob = trackMediaBlob(new Blob(['test']), '7002@home.example');
    createMediaObjectURL(blob);
    revokeMedia(['7002@home.example']);
    expect(revoke).toHaveBeenCalledWith('blob:revoked');
    await expect(saveMediaBlob(blob, 'test.txt')).rejects.toThrow('no longer available');
    releaseMedia(['7002@home.example']);
    await expect(saveMediaBlob(blob, 'test.txt')).rejects.toThrow('downloaded again');
    vi.restoreAllMocks();
  });

  it('aborts pending requests and rejects a late response even if transport ignores cancellation', async () => {
    let resolve!: (value: Response) => void;
    const fetch = vi.fn(
      (_path: string, _init: RequestInit) =>
        new Promise<Response>((done) => {
          resolve = done;
        })
    );
    vi.stubGlobal('fetch', fetch);
    const pending = authenticatedMediaBlob({
      path: '/media/home.example/7003/original',
      contentType: 'image/png'
    });
    const rejection = expect(pending).rejects.toThrow('cancelled');
    revokeMedia(['7003@home.example']);
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
    resolve(new Response(new Uint8Array([1, 2, 3])));
    await rejection;
    await expect(
      authenticatedMediaBlob({
        path: '/media/home.example/7003/original',
        contentType: 'image/png'
      })
    ).rejects.toThrow('no longer available');
    expect(fetch).toHaveBeenCalledOnce();
  });
});
