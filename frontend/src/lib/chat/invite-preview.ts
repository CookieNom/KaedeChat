import { api } from '$lib/api/client';
import type { Guild } from './types';

export interface InvitePreview {
  code: string;
  guild: Guild;
  expires_at: string | null;
}

interface CacheEntry {
  expiresAt: number;
  result: Promise<InvitePreview>;
}

const previewCache = new Map<string, CacheEntry>();
const SUCCESS_TTL_MS = 5 * 60_000;
const FAILURE_TTL_MS = 15_000;

export function loadInvitePreview(reference: string): Promise<InvitePreview> {
  const now = Date.now();
  const cached = previewCache.get(reference);
  if (cached && cached.expiresAt > now) return cached.result;
  if (previewCache.size >= 256) {
    const oldest = previewCache.keys().next().value;
    if (oldest) previewCache.delete(oldest);
  }

  const entry: CacheEntry = {
    expiresAt: now + SUCCESS_TTL_MS,
    result: api<InvitePreview>(`/invites/${encodeURIComponent(reference)}`).catch((error) => {
      entry.expiresAt = Date.now() + FAILURE_TTL_MS;
      throw error;
    })
  };
  previewCache.set(reference, entry);
  return entry.result;
}
