import { afterEach, describe, expect, it, vi } from 'vitest';

import { uploadObject, type PendingUpload, type UploadTicket } from './uploads';

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
  afterEach(() => vi.unstubAllGlobals());

  it('keeps API snowflakes as strings', () => {
    expect(typeof ticket().id).toBe('string');
  });

  it('models progress as an explicit lifecycle', () => {
    const upload = {
      key: 'one',
      file: new File(['x'], 'x.txt'),
      progress: 100,
      status: 'ready',
      attachmentId: '1'
    } satisfies PendingUpload;
    expect(upload.status).toBe('ready');
  });

  it('aborts an in-flight object upload with its route signal', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const controller = new AbortController();
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.png', { type: 'image/png' }),
      vi.fn(),
      controller.signal
    );

    controller.abort();

    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(FakeXMLHttpRequest.latest?.aborted).toBe(true);
  });

  it('gives connection guidance when media storage is unreachable', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.png', { type: 'image/png' }),
      vi.fn()
    );

    FakeXMLHttpRequest.latest?.onerror?.();

    await expect(request).rejects.toThrow(
      'Could not reach media storage. Check your connection and try again.'
    );
  });

  it('explains an expired or rejected signed upload instead of showing only a status code', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
    const request = uploadObject(
      ticket(),
      new File(['content'], 'paper.png', { type: 'image/png' }),
      vi.fn()
    );
    if (FakeXMLHttpRequest.latest) FakeXMLHttpRequest.latest.status = 403;

    FakeXMLHttpRequest.latest?.onload?.();

    await expect(request).rejects.toThrow(
      'Media storage rejected the upload authorization. Choose the file again and retry.'
    );
  });
});
