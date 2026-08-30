type ScannedResponse = Record<string, unknown>;

export interface ScannedMediaOptions {
  signal?: AbortSignal;
  delayMs?: number;
  maxAttempts?: number;
  rejectedMessage?: string;
  timeoutMessage?: string;
}

function scanStatus(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'pending';
  const response = value as ScannedResponse;
  if (typeof response.scan_status === 'string') return response.scan_status;
  const attachment = response.attachment;
  return attachment && typeof attachment === 'object' && !Array.isArray(attachment)
    ? typeof (attachment as ScannedResponse).scan_status === 'string'
      ? ((attachment as ScannedResponse).scan_status as string)
      : 'pending'
    : 'pending';
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(new DOMException('Request cancelled', 'AbortError'));
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timeout);
        reject(new DOMException('Request cancelled', 'AbortError'));
      },
      { once: true }
    );
  });
}

/**
 * Poll a scan through the same authority-qualified, idempotent commit route.
 * Upload tickets for federated resources live at the resource authority, so a
 * local `/attachments/{id}` lookup is neither authoritative nor collision-safe.
 */
export async function completeScannedMediaResource<R, T extends R>(
  commit: () => Promise<R>,
  isComplete: (value: R) => value is T,
  options: ScannedMediaOptions = {}
): Promise<T> {
  const maxAttempts = options.maxAttempts ?? 45;
  const delayMs = options.delayMs ?? 1_000;
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1) {
    throw new RangeError('Media scan attempts must be a positive integer.');
  }
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const result = await commit();
    if (isComplete(result)) return result;
    const status = scanStatus(result);
    if (['rejected', 'infected', 'failed'].includes(status)) {
      throw new Error(
        options.rejectedMessage ??
          'The selected media did not pass media safety processing. Choose another file.'
      );
    }
    if (attempt + 1 < maxAttempts) await delay(delayMs, options.signal);
  }
  throw new Error(
    options.timeoutMessage ?? 'Media processing is taking longer than expected. Try again shortly.'
  );
}
