/** Run in the browser. Obtain targets through your own authenticated backend;
 * never put a Cinnamon project secret in browser code. */
export interface RegionalProbe {
  region: string;
  probe_url: string;
}
export interface ProbeDiscovery {
  regions: RegionalProbe[];
  max_age_seconds: number;
}

/** Warm each connection, then take the median of three HTTPS round trips.
 * Six concurrent targets, 1.5s per request, 8s overall. Failed targets are omitted.
 * HTTPS RTT is a routing hint, not a measurement of WebRTC UDP quality. */
export async function measureRegionalLatency(
  discovery: ProbeDiscovery,
  fetcher: typeof fetch = fetch
): Promise<Record<string, number>> {
  const results: Record<string, number> = Object.create(null);
  const deadline = performance.now() + 8000;
  let index = 0;
  const targets = discovery.regions.slice(0, 64);
  async function worker() {
    while (index < targets.length && performance.now() < deadline) {
      const target = targets[index++];
      if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(target.region)) continue;
      let url: URL;
      try {
        url = new URL(target.probe_url);
      } catch {
        continue;
      }
      if (
        url.protocol !== 'https:' ||
        url.username ||
        url.password ||
        url.port ||
        url.pathname !== '/rtc-probe' ||
        url.search ||
        url.hash
      )
        continue;
      const samples: number[] = [];
      for (let sample = 0; sample < 4 && performance.now() < deadline; sample++) {
        const controller = new AbortController();
        const timer = setTimeout(
          () => controller.abort(),
          Math.min(1500, deadline - performance.now())
        );
        try {
          const start = performance.now();
          const response = await fetcher(url.toString(), {
            method: 'GET',
            mode: 'cors',
            credentials: 'omit',
            cache: 'no-store',
            redirect: 'error',
            referrerPolicy: 'no-referrer',
            signal: controller.signal
          });
          const elapsed = performance.now() - start;
          if (response.status !== 204 || response.headers.get('x-cinnamon-rtc-probe') !== '1')
            break;
          if (sample > 0) samples.push(elapsed);
        } catch {
          /* Stop retrying an unreachable target on the join path. */
          break;
        } finally {
          clearTimeout(timer);
        }
      }
      if (samples.length >= 2) {
        samples.sort((a, b) => a - b);
        const middle = Math.floor(samples.length / 2);
        const median =
          samples.length % 2 ? samples[middle] : (samples[middle - 1] + samples[middle]) / 2;
        results[target.region] = Math.max(1, Math.ceil(median));
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(6, targets.length) }, () => worker()));
  return results;
}

// Reuse only recent browser measurements; callers still obtain fresh probe tickets.
let recent:
  | {
      key: string;
      expires: number;
      result: Promise<Record<string, number>>;
    }
  | undefined;

export function recentRegionalLatency(discovery: ProbeDiscovery): Promise<Record<string, number>> {
  const key = JSON.stringify(discovery.regions);
  const now = performance.now();
  if (recent?.key === key && now < recent.expires) return recent.result;
  const result = measureRegionalLatency(discovery);
  recent = {
    key,
    expires: now + Math.min(45, Math.max(0, discovery.max_age_seconds)) * 1000,
    result
  };
  return result;
}
