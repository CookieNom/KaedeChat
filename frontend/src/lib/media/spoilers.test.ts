import { describe, expect, it, vi, beforeEach } from 'vitest';
import { api } from '$lib/api/client';
import { createUploadQueue } from './upload-queue';
import { isAttachmentSpoiler, spoilerFilename } from './spoilers';
import type { PendingUpload, UploadTicket } from './uploads';
import type { EncryptedFileManifest } from '$lib/e2ee/media';

vi.mock('$lib/api/client', () => ({ api: vi.fn() }));
const ticket = { id: '123', filename: 'photo.png' } as UploadTicket;
function setup(initial: PendingUpload[] = []) {
  let items = initial;
  const queue = createUploadQueue(
    () => items,
    (next) => {
      items = next;
    }
  );
  return { queue, read: () => items };
}
const file = () => new File(['image'], 'photo.png', { type: 'image/png' });
beforeEach(() => {
  vi.mocked(api).mockReset().mockResolvedValue({});
});

describe('attachment spoiler metadata', () => {
  it('normalizes prefixes and retains extensions at the filename limit', () => {
    expect(spoilerFilename('SPOILER_SPOILER_photo.png', false)).toBe('photo.png');
    expect(spoilerFilename('SPOILER_photo.png', true)).toBe('SPOILER_photo.png');
    const long = spoilerFilename('a'.repeat(251) + '.png', true);
    expect(long.length).toBe(255);
    expect(long.endsWith('.png')).toBe(true);
    expect(isAttachmentSpoiler(long)).toBe(true);
  });

  it('applies changes made during upload before exposing a sendable attachment', async () => {
    const { queue, read } = setup();
    let finish!: (ticket: UploadTicket) => void;
    queue.add(
      file(),
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
      () => true
    );
    await queue.setSpoiler(read()[0].key, true);
    expect(read()[0].status).toBe('uploading');
    finish(ticket);
    await vi.waitFor(() => expect(read()[0].status).toBe('ready'));
    expect(api).toHaveBeenCalledWith('/attachments/123/spoiler', {
      method: 'PATCH',
      body: JSON.stringify({ spoiler: true })
    });
    expect(read()[0].file.name).toBe('SPOILER_photo.png');
    await queue.setSpoiler(read()[0].key, false);
    expect(read()[0].file.name).toBe('photo.png');
    expect(read()[0].attachmentId).toBe('123');
  });

  it('keeps failed spoiler changes unsendable and lets the user retry', async () => {
    const { queue, read } = setup([
      { key: 'a', file: file(), progress: 100, status: 'ready', attachmentId: '123' }
    ]);
    vi.mocked(api).mockRejectedValueOnce(new Error('offline'));
    await queue.setSpoiler('a', true);
    expect(read()[0].status).toBe('failed');
    expect(read()[0].file.name).toBe('SPOILER_photo.png');
    await queue.setSpoiler('a', true);
    expect(read()[0].status).toBe('ready');
  });

  it('changes encrypted manifests without disclosing names or flags to the server', async () => {
    const manifest = { filename: 'photo.png', file_id: 'secret-file' } as EncryptedFileManifest;
    const { queue, read } = setup();
    queue.add(
      file(),
      async () => ({ ticket: { ...ticket, filename: 'encrypted-file' }, manifest }),
      () => true
    );
    await vi.waitFor(() => expect(read()[0].status).toBe('ready'));
    await queue.setSpoiler(read()[0].key, true);
    expect(read()[0].encryptedManifest?.filename).toBe('SPOILER_photo.png');
    expect(api).not.toHaveBeenCalled();
  });

  it('supports encrypted forum files deferred until thread creation', async () => {
    const { queue, read } = setup([{ key: 'a', file: file(), progress: 100, status: 'ready' }]);
    await queue.setSpoiler('a', true);
    expect(read()[0].file.name).toBe('SPOILER_photo.png');
    expect(read()[0].status).toBe('ready');
    expect(api).not.toHaveBeenCalled();
  });

  it('does not restore removed uploads when a metadata request finishes', async () => {
    const { queue, read } = setup([
      { key: 'a', file: file(), progress: 100, status: 'ready', attachmentId: '123' }
    ]);
    let finish!: () => void;
    vi.mocked(api).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = () => resolve({});
        })
    );
    const saving = queue.setSpoiler('a', true);
    expect(read()[0].status).toBe('uploading');
    queue.remove('a');
    finish();
    await saving;
    expect(read()).toEqual([]);
  });
  it('finishes command attachment selection after retrying a failed initial spoiler update', async () => {
    const { queue, read } = setup();
    const onReady = vi.fn();
    let finish!: (ticket: UploadTicket) => void;
    queue.add(
      file(),
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
      () => true,
      onReady
    );
    await queue.setSpoiler(read()[0].key, true);
    vi.mocked(api).mockRejectedValueOnce(new Error('offline'));
    finish(ticket);
    await vi.waitFor(() => expect(read()[0].status).toBe('failed'));
    expect(onReady).not.toHaveBeenCalled();
    await queue.setSpoiler(read()[0].key, true);
    expect(onReady).toHaveBeenCalledExactlyOnceWith('123');
    expect(read()[0].status).toBe('ready');
  });
});
