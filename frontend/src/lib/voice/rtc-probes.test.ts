import { describe, expect, it, vi } from 'vitest';
import { measureRegionalLatency } from './rtc-probes';

describe('RTC latency hints', () => {
  it('omits failed and unsafe probes, requires the probe marker, and sends no credentials', async () => {
    const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
      expect(options).toMatchObject({
        credentials: 'omit',
        redirect: 'error',
        cache: 'no-store',
        referrerPolicy: 'no-referrer'
      });
      if (url.includes('failed')) throw new Error('unreachable');
      return new Response(null, {
        status: 204,
        headers: url.includes('unmarked') ? {} : { 'X-Cinnamon-RTC-Probe': '1' }
      });
    });
    const result = await measureRegionalLatency(
      {
        regions: [
          { region: 'west', probe_url: 'https://west.example.com/rtc-probe' },
          { region: 'failed', probe_url: 'https://failed.example.com/rtc-probe' },
          { region: 'unmarked', probe_url: 'https://unmarked.example.com/rtc-probe' },
          { region: 'unsafe', probe_url: 'http://unsafe.example.com/rtc-probe' }
        ],
        max_age_seconds: 60
      },
      fetcher as typeof fetch
    );
    expect(Object.keys(result)).toEqual(['west']);
    expect(result.west).toBeGreaterThan(0);
    expect(fetcher).toHaveBeenCalledTimes(12);
  });
});
