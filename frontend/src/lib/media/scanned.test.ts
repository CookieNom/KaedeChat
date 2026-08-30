import { describe, expect, it, vi } from 'vitest';
import { completeScannedMediaResource } from './scanned';

describe('federated scanned media lifecycle', () => {
  it('retries the same authority-scoped commit instead of a local attachment lookup', async () => {
    const commit = vi
      .fn<() => Promise<Record<string, unknown>>>()
      .mockResolvedValueOnce({
        status: 'processing',
        attachment: { id: '7', origin_domain: 'remote.example', scan_status: 'pending' }
      })
      .mockResolvedValueOnce({
        status: 'processing',
        attachment: { id: '7', origin_domain: 'remote.example', scan_status: 'pending' }
      })
      .mockResolvedValueOnce({ application_ref: '9@remote.example', name: 'ready' });

    const result = await completeScannedMediaResource(
      commit,
      (value): value is Record<string, unknown> & { application_ref: string } =>
        typeof value.application_ref === 'string',
      { delayMs: 0 }
    );

    expect(result.application_ref).toBe('9@remote.example');
    expect(commit).toHaveBeenCalledTimes(3);
  });

  it('fails closed on a nested terminal scan response', async () => {
    await expect(
      completeScannedMediaResource(
        async () => ({ attachment: { scan_status: 'infected' } }),
        (value): value is { attachment: { scan_status: string }; complete: true } =>
          'complete' in value && value.complete === true,
        { delayMs: 0 }
      )
    ).rejects.toThrow(/did not pass/u);
  });
});
