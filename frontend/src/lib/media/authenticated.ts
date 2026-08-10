import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

export interface AuthenticatedMediaSource {
  path: string;
  contentType: string;
}

type MediaElement = HTMLImageElement | HTMLVideoElement;

function asBytes(value: ArrayBuffer | Uint8Array | number[]): Uint8Array {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  return Uint8Array.from(value);
}

function asBlobPart(value: ArrayBuffer | Uint8Array | number[]): ArrayBuffer {
  const bytes = asBytes(value);
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

export function attachmentMediaPath(
  originDomain: string,
  attachmentId: string,
  variant: 'original' | 'thumbnail_128' | 'thumbnail_512' | 'thumbnail_1024' | 'poster'
): string {
  return `/media/${encodeURIComponent(originDomain)}/${encodeURIComponent(attachmentId)}/${variant}`;
}

export function authenticatedMedia(node: MediaElement, source: AuthenticatedMediaSource) {
  let generation = 0;
  let objectUrl = '';

  function revokeObjectUrl() {
    if (!objectUrl) return;
    URL.revokeObjectURL(objectUrl);
    objectUrl = '';
  }

  async function load(next: AuthenticatedMediaSource) {
    const current = ++generation;
    revokeObjectUrl();
    node.removeAttribute('data-media-error');

    if (!isNativeDesktop()) {
      node.src = next.path;
      if (node instanceof HTMLVideoElement) node.load();
      return;
    }

    node.removeAttribute('src');
    try {
      const response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>(
        'native_media_request',
        { path: next.path }
      );
      if (current !== generation) return;
      objectUrl = URL.createObjectURL(new Blob([asBlobPart(response)], { type: next.contentType }));
      node.src = objectUrl;
      if (node instanceof HTMLVideoElement) node.load();
    } catch {
      if (current === generation) node.setAttribute('data-media-error', 'true');
    }
  }

  void load(source);
  return {
    update(next: AuthenticatedMediaSource) {
      void load(next);
    },
    destroy() {
      generation += 1;
      revokeObjectUrl();
    }
  };
}

export async function downloadAuthenticatedMedia(
  source: AuthenticatedMediaSource,
  filename: string
): Promise<void> {
  if (!isNativeDesktop()) {
    const anchor = document.createElement('a');
    anchor.href = source.path;
    anchor.download = filename;
    anchor.click();
    return;
  }

  const response = await nativeInvoke<ArrayBuffer | Uint8Array | number[]>('native_media_request', {
    path: source.path
  });
  const objectUrl = URL.createObjectURL(
    new Blob([asBlobPart(response)], { type: source.contentType })
  );
  try {
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
