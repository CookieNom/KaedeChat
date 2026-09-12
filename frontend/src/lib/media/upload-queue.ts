import { api } from '$lib/api/client';
import { isAttachmentSpoiler, spoilerFilename } from './spoilers';
import { userErrorMessage } from '$lib/api/errors';
import type { EncryptedFileManifest } from '$lib/e2ee/media';
import type { PendingUpload, UploadTicket } from './uploads';

type UploadResult = UploadTicket | { ticket: UploadTicket; manifest: EncryptedFileManifest };

/** Own cancellation/progress while each caller keeps its route and permission rules. */
export function createUploadQueue(
  read: () => PendingUpload[],
  write: (uploads: PendingUpload[]) => void
) {
  const controllers = new Map<string, AbortController>();
  const readyCallbacks = new Map<string, (attachmentId: string) => void>();
  async function finishSpoiler(key: string, result: UploadResult, isCurrent: () => boolean) {
    const item = read().find((item) => item.key === key);
    if (!item || !isCurrent()) return;
    const ticket = 'ticket' in result ? result.ticket : result;
    const spoiler = isAttachmentSpoiler(item.file.name);
    write(
      read().map((entry) =>
        entry.key === key ? { ...entry, updating: true, attachmentId: ticket.id } : entry
      )
    );
    if (!('manifest' in result) && spoiler !== isAttachmentSpoiler(ticket.filename)) {
      await api(`/attachments/${encodeURIComponent(ticket.id)}/spoiler`, {
        method: 'PATCH',
        body: JSON.stringify({ spoiler })
      });
    }
    if (!isCurrent()) return;
    write(
      read().map((entry) =>
        entry.key === key
          ? {
              ...entry,
              progress: 100,
              status: 'ready',
              updating: false,
              attachmentId: ticket.id,
              ...('manifest' in result
                ? {
                    encryptedManifest: {
                      ...result.manifest,
                      filename: spoilerFilename(result.manifest.filename, spoiler)
                    }
                  }
                : {})
            }
          : entry
      )
    );
  }
  return {
    async setSpoiler(key: string, spoiler: boolean) {
      const item = read().find((item) => item.key === key);
      if (!item || item.updating) return;
      const file = new File([item.file], spoilerFilename(item.file.name, spoiler), {
        type: item.file.type,
        lastModified: item.file.lastModified
      });
      const ready = !!item.attachmentId;
      write(
        read().map((entry) =>
          entry.key === key
            ? {
                ...entry,
                file,
                ...(ready ? { status: 'uploading', updating: true } : {})
              }
            : entry
        )
      );
      if (!ready) return;
      try {
        if (!item.encryptedManifest) {
          await api(`/attachments/${encodeURIComponent(item.attachmentId!)}/spoiler`, {
            method: 'PATCH',
            body: JSON.stringify({ spoiler })
          });
        }
        write(
          read().map((entry) =>
            entry.key === key
              ? {
                  ...entry,
                  status: 'ready',
                  updating: false,
                  error: undefined,
                  ...(item.encryptedManifest
                    ? {
                        encryptedManifest: {
                          ...item.encryptedManifest,
                          filename: spoilerFilename(item.encryptedManifest.filename, spoiler)
                        }
                      }
                    : {})
                }
              : entry
          )
        );
        if (read().some((entry) => entry.key === key)) {
          readyCallbacks.get(key)?.(item.attachmentId!);
          readyCallbacks.delete(key);
        }
      } catch (caught) {
        write(
          read().map((entry) =>
            entry.key === key
              ? {
                  ...entry,
                  status: 'failed',
                  updating: false,
                  error: userErrorMessage(
                    caught,
                    'Could not change spoiler. Toggle again to retry.'
                  )
                }
              : entry
          )
        );
      }
    },
    add(
      file: File,
      upload: (progress: (value: number) => void, signal: AbortSignal) => Promise<UploadResult>,
      isCurrent: () => boolean,
      onReady?: (attachmentId: string) => void
    ) {
      const key = crypto.randomUUID();
      const controller = new AbortController();
      if (onReady) readyCallbacks.set(key, onReady);
      controllers.set(key, controller);
      write([...read(), { key, file, progress: 0, status: 'uploading' }]);
      void upload((progress) => {
        if (controller.signal.aborted || !isCurrent()) return;
        write(read().map((item) => (item.key === key ? { ...item, progress } : item)));
      }, controller.signal)
        .then(async (result) => {
          if (controller.signal.aborted || !isCurrent()) return;
          await finishSpoiler(key, result, () => !controller.signal.aborted && isCurrent());
          controllers.delete(key);
          if (controller.signal.aborted || !isCurrent() || !read().some((item) => item.key === key))
            return;
          readyCallbacks.get(key)?.('ticket' in result ? result.ticket.id : result.id);
          readyCallbacks.delete(key);
        })
        .catch((caught: unknown) => {
          controllers.delete(key);
          if (controller.signal.aborted || !isCurrent()) return;
          write(
            read().map((item) =>
              item.key === key
                ? {
                    ...item,
                    status: 'failed',
                    updating: false,
                    error: userErrorMessage(caught, 'Upload failed. Remove the file and try again.')
                  }
                : item
            )
          );
        });
    },
    remove(key: string) {
      readyCallbacks.delete(key);
      controllers.get(key)?.abort();
      controllers.delete(key);
      write(read().filter((item) => item.key !== key));
    },
    reset() {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      readyCallbacks.clear();
      write([]);
    }
  };
}
