import { describe, expect, it, vi } from 'vitest';
import {
  attachmentMediaPath,
  copyAuthenticatedImage,
  dmHistoryAttachmentMediaPath,
  downloadAuthenticatedMedia,
  saveMediaBlob,
  privateInteractionAttachmentMediaPath,
  isSafeSameOriginMediaPath,
  mediaCapacityRetryDelay
} from './authenticated';

describe('authenticated image clipboard', () => {
  it('copies fetched image bytes through the platform clipboard', async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { write } });
    vi.stubGlobal(
      'ClipboardItem',
      class {
        constructor(readonly data: Record<string, Blob>) {}
      }
    );
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), { status: 200 }))
    );
    try {
      await copyAuthenticatedImage({ path: '/media/image/original', contentType: 'image/png' });
      expect(write).toHaveBeenCalledOnce();
      const [items] = write.mock.calls[0] as [{ data: Record<string, Blob> }[]];
      expect(items).toHaveLength(1);
      expect(Object.keys(items[0].data)).toEqual(['image/png']);
      expect(items[0].data['image/png'].type).toBe('image/png');
      expect(new Uint8Array(await items[0].data['image/png'].arrayBuffer())).toEqual(
        new Uint8Array([1, 2, 3])
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('authenticated media download', () => {
  it('saves desktop attachments through the native dialog with the filename and original bytes', async () => {
    const bytes = new Uint8Array([0, 1, 255]);
    const invoke = vi.fn().mockResolvedValue(undefined);
    const createElement = vi.fn();
    vi.stubGlobal('window', { __TAURI__: { core: { invoke } } });
    vi.stubGlobal('document', { createElement });
    try {
      await saveMediaBlob(new Blob([bytes]), '写真.png');
      const [command, payload] = invoke.mock.calls[0] as [string, Uint8Array];
      expect(command).toBe('native_save_attachment');
      const length = new DataView(payload.buffer).getUint32(0, true);
      expect(new TextDecoder().decode(payload.slice(4, 4 + length))).toBe('写真.png');
      expect(payload.slice(4 + length)).toEqual(bytes);
      expect(createElement).not.toHaveBeenCalled();

      invoke.mockRejectedValueOnce(new Error('Cannot save file'));
      await expect(saveMediaBlob(new Blob([bytes]), 'image.png')).rejects.toThrow(
        'Cannot save file'
      );
      expect(createElement).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('blob-URL download behavior', async () => {
    const click = vi.fn();
    const anchor = { href: '', download: '', click };
    const createObjectURL = vi.fn().mockReturnValue('blob:download');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3]))));
    vi.stubGlobal('document', { createElement: vi.fn().mockReturnValue(anchor) });
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() });
    vi.stubGlobal('window', { setTimeout: vi.fn() });
    try {
      await downloadAuthenticatedMedia(
        { path: '/media/image/original', contentType: 'image/png' },
        'image.png'
      );
      expect(anchor).toMatchObject({ href: 'blob:download', download: 'image.png' });
      expect(createObjectURL).toHaveBeenCalledOnce();
      expect(click).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('authenticated media capacity retry', () => {
  it('uses bounded Retry-After timing only for declared temporary capacity errors', () => {
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', '1', 0, '')).toBe(1000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_CACHE_FULL', '30', 0, '')).toBe(5000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_UNAVAILABLE', '1', 0, '')).toBeNull();
    expect(mediaCapacityRetryDelay(404, 'REMOTE_MEDIA_BUSY', '1', 0, '')).toBeNull();
  });

  it('bounds automatic retries so failures still reach the manual Retry state', () => {
    const delays = Array.from({ length: 10 }, (_, attempt) =>
      mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', null, attempt, '')
    );
    const stopped = delays.indexOf(null);
    expect(stopped).toBeGreaterThan(0);
    expect(stopped).toBeLessThan(delays.length);
    for (const delay of delays.slice(0, stopped)) expect(delay).toBeGreaterThan(0);
    expect(delays.slice(stopped).every((delay) => delay === null)).toBe(true);
  });
});

describe('federated history media paths', () => {
  it('prefers a temporary same-origin authenticated history path', () => {
    const path =
      '/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=1787961600&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO';
    expect(attachmentMediaPath('remote.example', '60', 'original', path)).toBe(path);
  });

  it('preserves an expired capability for downstream handling', () => {
    const expired =
      '/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=1&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO';
    expect(attachmentMediaPath('remote.example', '60', 'original', expired)).toBe(expired);
  });

  it('rejects cross-host and backslash URL forms before falling back', () => {
    expect(isSafeSameOriginMediaPath('https://evil.example/media')).toBe(false);
    expect(isSafeSameOriginMediaPath('//evil.example/media')).toBe(false);
    expect(isSafeSameOriginMediaPath('/\\evil.example/media')).toBe(false);
    expect(attachmentMediaPath('remote.example', '123', 'original', '//evil.example/media')).toBe(
      '/media/remote.example/123/original'
    );
  });

  it('rejects same-origin history paths with the wrong route, identity, variant, or query', () => {
    const valid =
      '/api/v1/dms/43@home.example/history-media/50@remote.example/60@remote.example/original?expires=1787961600&token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO';
    expect(dmHistoryAttachmentMediaPath('remote.example', '60', 'original', valid)).toBe(valid);
    expect(
      dmHistoryAttachmentMediaPath('remote.example', '60', 'original', '/api/v1/users/@me')
    ).toBeNull();
    expect(dmHistoryAttachmentMediaPath('remote.example', '61', 'original', valid)).toBeNull();
    expect(dmHistoryAttachmentMediaPath('remote.example', '60', 'thumbnail_512', valid)).toBeNull();
    expect(
      dmHistoryAttachmentMediaPath('remote.example', '60', 'original', `${valid}&next=/users/@me`)
    ).toBeNull();
  });
});

describe('private interaction media paths', () => {
  const base =
    '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/90@chat.example';

  it('binds the projected path to every qualified identity', () => {
    expect(privateInteractionAttachmentMediaPath('chat.example', '90', 'original', base)).toBe(
      `${base}/original`
    );
    expect(attachmentMediaPath('chat.example', '90', 'thumbnail_512', null, base)).toBe(
      `${base}/thumbnail_512`
    );
    expect(
      privateInteractionAttachmentMediaPath('chat.example', '91', 'original', base)
    ).toBeNull();
    expect(
      privateInteractionAttachmentMediaPath(
        'chat.example',
        '90',
        'original',
        base.replace('71@chat.example', '71@other.example')
      )
    ).toBeNull();
  });

  it('never accepts a remote or malformed override', () => {
    expect(
      privateInteractionAttachmentMediaPath(
        'chat.example',
        '90',
        'original',
        'https://evil.example/private'
      )
    ).toBeNull();
    expect(
      privateInteractionAttachmentMediaPath(
        'chat.example',
        '90',
        'original',
        base.replace('chat.example', 'chat..example')
      )
    ).toBeNull();
  });
});
