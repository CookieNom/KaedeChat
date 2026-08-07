interface FederatedAssetOwner {
  origin_domain: string;
}

const CONTENT_HASH = /^[0-9a-f]{64}$/;
const VARIANT = /^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$/;
const FEDERATION_DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;

export function assetUrl(
  contentHash: string,
  variant: string,
  owner?: FederatedAssetOwner | string | null
): string {
  if (!CONTENT_HASH.test(contentHash) || !VARIANT.test(variant)) return '';
  const domain = typeof owner === 'string' ? owner : owner?.origin_domain;
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname.toLowerCase();
  const path = `/media/assets/${contentHash}/${variant}`;
  if (!domain || domain.toLowerCase() === localDomain) return path;
  if (!FEDERATION_DOMAIN.test(domain)) return '';
  return `https://${domain.toLowerCase()}${path}`;
}
