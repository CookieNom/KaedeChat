import { afterEach, describe, expect, it, vi } from 'vitest';
import { measureRegionalLatency, recentRegionalLatency } from './rtc-probes';

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
    expect(fetcher).toHaveBeenCalledTimes(6);
  });
});

describe('recent RTC latency measurements', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shares background work, expires measurements, and remeasures changed targets', async () => {
    const clock = vi.spyOn(performance, 'now').mockReturnValue(1000);
    const fetcher = vi.fn(
      async () =>
        new Response(null, {
          status: 204,
          headers: { 'X-Cinnamon-RTC-Probe': '1' }
        })
    );
    vi.stubGlobal('fetch', fetcher);
    const discovery = {
      regions: [{ region: 'east', probe_url: 'https://east.example.com/rtc-probe' }],
      max_age_seconds: 60
    };
    const background = recentRegionalLatency(discovery);
    expect(recentRegionalLatency(discovery)).toBe(background);
    await background;
    await recentRegionalLatency(discovery);
    expect(fetcher).toHaveBeenCalledTimes(4);
    clock.mockReturnValue(47000);
    await recentRegionalLatency(discovery);
    expect(fetcher).toHaveBeenCalledTimes(8);
    await recentRegionalLatency({
      ...discovery,
      regions: [{ region: 'east', probe_url: 'https://replacement.example.com/rtc-probe' }]
    });
    expect(fetcher).toHaveBeenCalledTimes(12);
  });
});
