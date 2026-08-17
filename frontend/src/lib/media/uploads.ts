import { api } from '$lib/api/client';
import { isNativeDesktop, nativeInvokeBytes } from '$lib/platform/native';
import type { EncryptedFileManifest } from '$lib/e2ee/media';

export interface UploadTicket {
  id: string;
  origin_domain: string;
  filename: string;
  content_type: string;
  size: number;
  upload_url: string;
  upload_method: 'PUT';
  upload_headers?: Record<string, string>;
  expires_at: string;
}

export interface PendingUpload {
  key: string;
  file: File;
  progress: number;
  status: 'uploading' | 'ready' | 'failed';
  attachmentId?: string;
  encryptedManifest?: EncryptedFileManifest;
  error?: string;
}

function uploadStatusMessage(status: number): string {
  if (status === 401 || status === 403) {
    return 'Media storage rejected the upload authorization. Choose the file again and retry.';
  }
  if (status === 413) return 'The file is larger than media storage allows. Choose a smaller file.';
  if (status === 415) return 'Media storage does not support this file type. Choose another file.';
  if (status === 429) return 'Media storage is receiving too many uploads. Try again shortly.';
  if (status >= 500) return 'Media storage is temporarily unavailable. Try again shortly.';
  return 'Media storage could not accept the file. Choose the file again and retry.';
}

export function uploadObject(
  ticket: UploadTicket,
  file: File,
  onProgress: (progress: number) => void,
  signal?: AbortSignal
): Promise<void> {
  if (isNativeDesktop()) {
    return (async () => {
      if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError');
      onProgress(1);
      const ticketBytes = new TextEncoder().encode(JSON.stringify(ticket));
      const fileBytes = new Uint8Array(await file.arrayBuffer());
      const payload = new Uint8Array(4 + ticketBytes.length + fileBytes.length);
      new DataView(payload.buffer).setUint32(0, ticketBytes.length, true);
      payload.set(ticketBytes, 4);
      payload.set(fileBytes, 4 + ticketBytes.length);
      await nativeInvokeBytes('native_upload_object', payload);
      if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError');
      onProgress(100);
    })();
  }
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const finish = (action: () => void) => {
      signal?.removeEventListener('abort', abort);
      request.onload = null;
      request.onerror = null;
      request.ontimeout = null;
      request.onabort = null;
      action();
    };
    const abort = () => request.abort();
    if (signal?.aborted) {
      reject(new DOMException('Upload cancelled', 'AbortError'));
      return;
    }
    request.open('PUT', ticket.upload_url);
    request.timeout = 15 * 60 * 1000;
    request.setRequestHeader('Content-Type', ticket.content_type);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) finish(() => resolve());
      else finish(() => reject(new Error(uploadStatusMessage(request.status))));
    };
    request.onerror = () =>
      finish(() =>
        reject(new Error('Could not reach media storage. Check your connection and try again.'))
      );
    request.ontimeout = () =>
      finish(() =>
        reject(new Error('The upload took too long. Check your connection and try again.'))
      );
    request.onabort = () =>
      finish(() => reject(new DOMException('Upload cancelled', 'AbortError')));
    signal?.addEventListener('abort', abort, { once: true });
    request.send(file);
  });
}

export async function uploadChannelFile(
  channelRef: string,
  file: File,
  onProgress: (progress: number) => void,
  signal?: AbortSignal
): Promise<UploadTicket> {
  const ticket = await api<UploadTicket>(
    `/channels/${encodeURIComponent(channelRef)}/attachments`,
    {
      method: 'POST',
      signal,
      body: JSON.stringify({
        filename: file.name || 'upload',
        content_type: file.type || 'application/octet-stream',
        size: file.size
      })
    }
  );
  if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError');
  await uploadObject(ticket, file, onProgress, signal);
  onProgress(100);
  return ticket;
}
