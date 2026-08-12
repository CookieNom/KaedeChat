import { describe, expect, it } from 'vitest';
import {
  attachmentMediaPath,
  isSafeSameOriginMediaPath,
  mediaCapacityRetryDelay
} from './authenticated';

describe('authenticated media capacity retry', () => {
  it('uses bounded Retry-After timing only for declared temporary capacity errors', () => {
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', '1', 0, '')).toBe(1000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_CACHE_FULL', '30', 0, '')).toBe(5000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_UNAVAILABLE', '1', 0, '')).toBeNull();
    expect(mediaCapacityRetryDelay(404, 'REMOTE_MEDIA_BUSY', '1', 0, '')).toBeNull();
  });

  it('stops after two automatic retries so failures still reach the manual Retry state', () => {
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', null, 0, '')).toBe(1000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', null, 1, '')).toBe(2000);
    expect(mediaCapacityRetryDelay(503, 'REMOTE_MEDIA_BUSY', null, 2, '')).toBeNull();
  });
});

describe('federated history media paths', () => {
  it('prefers a temporary same-origin authenticated history path', () => {
    expect(
      attachmentMediaPath('remote.example', '123', 'original', '/api/v1/dms/history/media/token')
    ).toBe('/api/v1/dms/history/media/token');
  });

  it('retries an expired signed path through the authenticated renewal route', () => {
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
});
