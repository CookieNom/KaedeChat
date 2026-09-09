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
  return {
    add(
      file: File,
      upload: (progress: (value: number) => void, signal: AbortSignal) => Promise<UploadResult>,
      isCurrent: () => boolean,
      onReady?: (attachmentId: string) => void
    ) {
      const key = crypto.randomUUID();
      const controller = new AbortController();
      controllers.set(key, controller);
      write([...read(), { key, file, progress: 0, status: 'uploading' }]);
      void upload((progress) => {
        if (controller.signal.aborted || !isCurrent()) return;
        write(read().map((item) => (item.key === key ? { ...item, progress } : item)));
      }, controller.signal)
        .then((result) => {
          controllers.delete(key);
          if (!isCurrent()) return;
          const attachmentId = 'ticket' in result ? result.ticket.id : result.id;
          write(
            read().map((item) =>
              item.key === key
                ? {
                    ...item,
                    progress: 100,
                    status: 'ready',
                    attachmentId,
                    ...('manifest' in result ? { encryptedManifest: result.manifest } : {})
                  }
                : item
            )
          );
          onReady?.(attachmentId);
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
                    error: userErrorMessage(caught, 'Upload failed. Remove the file and try again.')
                  }
                : item
            )
          );
        });
    },
    remove(key: string) {
      controllers.get(key)?.abort();
      controllers.delete(key);
      write(read().filter((item) => item.key !== key));
    },
    reset() {
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      write([]);
    }
  };
}
