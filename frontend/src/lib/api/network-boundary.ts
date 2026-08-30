const CLIENT_ONLY_FIELDS = new Set([
  'e2ee_verified',
  'decrypted_content',
  'decrypted_attachments',
  'decrypted_allowed_mentions',
  'decrypted_forward_snapshot',
  'encrypted_manifest'
]);

/**
 * Remove fields that can only be created after local authenticated decryption.
 * REST and Gateway peers are never allowed to assert this client state, even
 * when a compromised federated authority includes convincing-looking keys.
 */
export function stripNetworkClientState<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => stripNetworkClientState(item)) as T;
  }
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !CLIENT_ONLY_FIELDS.has(key))
      .map(([key, item]) => [key, stripNetworkClientState(item)])
  ) as T;
}
