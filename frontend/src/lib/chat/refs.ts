export interface FederatedIdentity {
  id: string;
  origin_domain: string;
}

/** Canonical API/browser reference for an ID that is only instance-unique. */
export function entityRef(entity: FederatedIdentity): string {
  return `${entity.id}@${entity.origin_domain}`;
}

/** Stable normalized-store/keyed-list identity for federated rows. */
export function entityKey(entity: FederatedIdentity): string {
  return entityRef(entity);
}

/**
 * Match a canonical route ref. The API accepts a bare snowflake only as local
 * shorthand, so a colliding remote entity must never match that legacy form.
 */
export function matchesEntityRef(
  ref: string,
  entity: FederatedIdentity,
  localDomain: string
): boolean {
  if (
    typeof ref !== 'string' ||
    typeof entity.id !== 'string' ||
    typeof entity.origin_domain !== 'string'
  ) {
    return false;
  }
  return (
    ref === entityRef(entity) ||
    (ref === entity.id && entity.origin_domain.toLowerCase() === localDomain.toLowerCase())
  );
}

export function sameEntity(left: FederatedIdentity, right: FederatedIdentity): boolean {
  return left.id === right.id && left.origin_domain === right.origin_domain;
}

/** Deterministic ordering for instance-local snowflakes carried with their home. */
export function compareEntityRefs(left: FederatedIdentity, right: FederatedIdentity): number {
  if (/^\d+$/.test(left.id) && /^\d+$/.test(right.id) && left.id !== right.id) {
    return BigInt(left.id) < BigInt(right.id) ? -1 : 1;
  }
  if (left.id !== right.id) return left.id.localeCompare(right.id);
  return left.origin_domain.localeCompare(right.origin_domain);
}
