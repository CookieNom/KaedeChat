import { afterEach, describe, expect, it, vi } from 'vitest';

import { uploadObject, type UploadTicket } from './uploads';

class FakeXMLHttpRequest {
  static latest: FakeXMLHttpRequest | null = null;
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;
  status = 0;
  timeout = 0;
  aborted = false;

  constructor() {
    FakeXMLHttpRequest.latest = this;
  }

  open(): void {}
  setRequestHeader(): void {}
  send(): void {}

  abort(): void {
    this.aborted = true;
    this.onabort?.();
  }
}

function ticket(): UploadTicket {
  return {
    id: '9223372036854775807',
    origin_domain: 'alpha.localhost',
    filename: 'paper.png',
    content_type: 'image/png',
    size: 8,
    upload_url: 'https://media.alpha.localhost/object',
    upload_method: 'PUT',
    expires_at: new Date(0).toISOString()
  };
}

describe('media upload contracts', () => {
  afterEach(() => {
    FakeXMLHttpRequest.latest = null;
    vi.unstubAllGlobals();
  });

  it('aborts an in-flight object upload with its route signal', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const controller = new AbortController();
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.bin', { type: 'application/octet-stream' }),
      vi.fn(),
      controller.signal
    );

    await vi.waitFor(() => expect(FakeXMLHttpRequest.latest).not.toBeNull());
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(FakeXMLHttpRequest.latest?.aborted).toBe(true);
  });

  it('gives connection guidance when media storage is unreachable', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.bin', { type: 'application/octet-stream' }),
      vi.fn()
    );

    await vi.waitFor(() => expect(FakeXMLHttpRequest.latest).not.toBeNull());

    FakeXMLHttpRequest.latest?.onerror?.();

    await expect(request).rejects.toThrow(/media storage.*connection.*(try|retry)/i);
  });

  it('explains an expired or rejected signed upload instead of showing only a status code', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.bin', { type: 'application/octet-stream' }),
      vi.fn()
    );
    await vi.waitFor(() => expect(FakeXMLHttpRequest.latest).not.toBeNull());
    if (FakeXMLHttpRequest.latest) FakeXMLHttpRequest.latest.status = 403;

    FakeXMLHttpRequest.latest?.onload?.();

    await expect(request).rejects.toThrow(/upload authorization.*file.*retry/i);
  });
});
