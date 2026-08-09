import { api } from '$lib/api/client';
import { isNativeDesktop, nativeInvokeBytes } from '$lib/platform/native';

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
  error?: string;
}

function storageEndpoint(ticket: UploadTicket): string {
  try {
    return new URL(ticket.upload_url).host;
  } catch {
    return 'the configured media host';
  }
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
      else finish(() => reject(new Error(`Object upload failed (${request.status})`)));
    };
    request.onerror = () =>
      finish(() =>
        reject(
          new Error(
            `Could not reach media storage at ${storageEndpoint(ticket)}. Check its DNS, TLS, and CORS configuration.`
          )
        )
      );
    request.ontimeout = () => finish(() => reject(new Error('Object upload timed out')));
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
