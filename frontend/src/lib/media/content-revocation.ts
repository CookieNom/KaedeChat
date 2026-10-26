import { parseCanonicalEntityRef } from '$lib/chat/refs';

const blocked = new Set<string>();
const listeners = new Set<(refs: ReadonlySet<string>) => void>();
const generations = new Map<string, number>();
const blobSources = new WeakMap<Blob, { ref: string; generation: number }>();
const objectUrls = new Map<string, string>();

export function mediaReference(path: string): string | null {
  let parts: string[];
  try {
    parts = new URL(path, 'https://kaede.invalid').pathname.split('/').map(decodeURIComponent);
  } catch {
    return null;
  }
  const media = parts.indexOf('media');
  if (media >= 0 && parseCanonicalEntityRef(`${parts[media + 2]}@${parts[media + 1]}`)) {
    return `${parts[media + 2]}@${parts[media + 1]}`;
  }
  const history = parts.indexOf('history-media');
  if (history >= 0 && parseCanonicalEntityRef(parts[history + 2])) return parts[history + 2];
  const attachment = parts.findLast((part) => parseCanonicalEntityRef(part));
  return attachment ?? null;
}

export function assertMediaAvailable(pathOrRef: string): void {
  const ref = parseCanonicalEntityRef(pathOrRef) ? pathOrRef : mediaReference(pathOrRef);
  if (ref && blocked.has(ref)) throw new Error('This attachment is no longer available.');
}

export function onMediaRevoked(listener: (refs: ReadonlySet<string>) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function revokeMedia(refs: readonly string[]): void {
  const valid = new Set(refs.filter((ref) => parseCanonicalEntityRef(ref)));
  for (const ref of valid) {
    blocked.add(ref);
    generations.set(ref, (generations.get(ref) ?? 0) + 1);
  }
  for (const [url, ref] of objectUrls) {
    if (valid.has(ref)) revokeMediaObjectURL(url);
  }
  for (const listener of listeners) listener(valid);
}

export function releaseMedia(refs: readonly string[]): void {
  for (const ref of refs) blocked.delete(ref);
}

export function resetMediaRestrictions(): void {
  blocked.clear();
}

export function trackMediaBlob(blob: Blob, pathOrRef: string): Blob {
  assertMediaAvailable(pathOrRef);
  const ref = parseCanonicalEntityRef(pathOrRef) ? pathOrRef : mediaReference(pathOrRef);
  if (ref) blobSources.set(blob, { ref, generation: generations.get(ref) ?? 0 });
  return blob;
}

export function assertMediaBlobAvailable(blob: Blob): void {
  const source = blobSources.get(blob);
  if (source) {
    assertMediaAvailable(source.ref);
    if ((generations.get(source.ref) ?? 0) !== source.generation) {
      throw new Error('This attachment must be downloaded again.');
    }
  }
}

export function createMediaObjectURL(blob: Blob): string {
  assertMediaBlobAvailable(blob);
  const url = URL.createObjectURL(blob);
  const source = blobSources.get(blob);
  if (source) objectUrls.set(url, source.ref);
  return url;
}

export function revokeMediaObjectURL(url: string): void {
  objectUrls.delete(url);
  URL.revokeObjectURL(url);
}
