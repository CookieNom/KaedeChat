import { isNativeDesktop, storedNativeInstance } from '$lib/platform/native';

interface FederatedAssetOwner {
  origin_domain: string;
}

const CONTENT_HASH = /^[0-9a-f]{64}$/;
const VARIANT = /^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$/;
// Increment when media derivatives change incompatibly. Public asset redirects
// are immutable, so this also moves repaired assets onto a fresh browser cache key.
export const MEDIA_ASSET_VERSION = 2;
const FEDERATION_DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;

export function assetUrl(
  contentHash: string,
  variant: string,
  owner?: FederatedAssetOwner | string | null
): string {
  if (!CONTENT_HASH.test(contentHash) || !VARIANT.test(variant)) return '';
  const suppliedDomain = typeof owner === 'string' ? owner : owner?.origin_domain;
  // The bundled Tauri UI is served from a loopback origin. An asset without an
  // explicit owner still belongs to the signed-in account's home instance, not
  // to that local webview server.
  const domain = suppliedDomain || (isNativeDesktop() ? storedNativeInstance() : '');
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname.toLowerCase();
  const path = `/media/assets/${contentHash}/${variant}?v=${MEDIA_ASSET_VERSION}`;
  if (!domain || domain.toLowerCase() === localDomain) return path;
  if (!FEDERATION_DOMAIN.test(domain)) return '';
  return `https://${domain.toLowerCase()}${path}`;
}
