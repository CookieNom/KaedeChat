import { userErrorMessage } from '$lib/api/client';
import { compareEntityRefs } from '$lib/chat/refs';

export interface ReadAcknowledgementTarget {
  id: string;
  origin_domain: string;
  channel_id: string;
  channel_domain: string;
}

interface ReadAcknowledgementQueueOptions<T extends ReadAcknowledgementTarget> {
  send: (message: T) => Promise<void>;
  acknowledged: (message: T) => void;
  warningChanged: (message: string) => void;
  warningThreshold?: number;
}

/**
 * Coalesces read acknowledgements so a transient outage cannot clear unread UI
 * before the server has accepted the update. Failures retry with bounded
 * backoff and become visible only after they persist.
 */
export class ReadAcknowledgementQueue<T extends ReadAcknowledgementTarget> {
  #pending: T | null = null;
  #inFlight: Promise<void> | null = null;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #failures = 0;
  #generation = 0;
  readonly #warningThreshold: number;

  constructor(private readonly options: ReadAcknowledgementQueueOptions<T>) {
    this.#warningThreshold = Math.max(1, options.warningThreshold ?? 3);
  }

  acknowledge(message: T): Promise<void> {
    if (!this.#pending || compareEntityRefs(this.#pending, message) < 0) this.#pending = message;
    if (this.#retryTimer) return Promise.resolve();
    return this.#flush();
  }

  retryNow(): Promise<void> {
    if (this.#retryTimer) clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
    return this.#flush();
  }

  reset(): void {
    this.#generation += 1;
    this.#pending = null;
    this.#failures = 0;
    if (this.#retryTimer) clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
    this.options.warningChanged('');
  }

  #flush(): Promise<void> {
    if (this.#inFlight) return this.#inFlight;
    const message = this.#pending;
    if (!message) return Promise.resolve();
    this.#pending = null;
    const generation = this.#generation;
    this.#inFlight = this.options
      .send(message)
      .then(() => {
        if (generation !== this.#generation) return;
        const superseded = this.#pending !== null && compareEntityRefs(message, this.#pending) < 0;
        if (superseded) return;
        this.#failures = 0;
        this.options.warningChanged('');
        this.options.acknowledged(message);
      })
      .catch((caught: unknown) => {
        if (generation !== this.#generation) return;
        if (!this.#pending || compareEntityRefs(this.#pending, message) < 0)
          this.#pending = message;
        this.#failures += 1;
        if (this.#failures >= this.#warningThreshold) {
          const reason = userErrorMessage(
            caught,
            'The server did not confirm the read update. Check your connection and try again.'
          );
          this.options.warningChanged(
            `Read state may be out of date. ${reason} Unread badges will remain until the server confirms them.`
          );
        }
        const delay = Math.min(1_000 * 2 ** (this.#failures - 1), 30_000);
        this.#retryTimer = setTimeout(() => {
          this.#retryTimer = null;
          void this.#flush();
        }, delay);
      })
      .finally(() => {
        this.#inFlight = null;
        if (this.#pending && !this.#retryTimer) {
          void this.#flush();
        }
      });
    return this.#inFlight;
  }
}
