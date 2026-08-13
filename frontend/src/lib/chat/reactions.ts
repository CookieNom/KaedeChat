const DEFAULT_RECENT_REACTIONS = ['❤️', '😂', '👍', '🔥'];
const STORAGE_PREFIX = 'kaede:reaction-recents:';

interface StoredReaction {
  value: string;
  count: number;
  lastUsed: number;
}

function storageKey(userKey: string): string {
  return `${STORAGE_PREFIX}${userKey || 'anonymous'}`;
}

function storedReactions(userKey: string): StoredReaction[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const value = JSON.parse(localStorage.getItem(storageKey(userKey)) ?? '[]');
    if (!Array.isArray(value)) return [];
    return value.filter(
      (item): item is StoredReaction =>
        typeof item?.value === 'string' &&
        item.value.length > 0 &&
        typeof item.count === 'number' &&
        typeof item.lastUsed === 'number'
    );
  } catch {
    return [];
  }
}

export function recentReactions(userKey: string, limit = 4): string[] {
  const stored = storedReactions(userKey)
    .sort((left, right) => right.count - left.count || right.lastUsed - left.lastUsed)
    .map((item) => item.value);
  return [...new Set([...stored, ...DEFAULT_RECENT_REACTIONS])].slice(0, limit);
}

export function rememberReaction(userKey: string, value: string): void {
  if (typeof localStorage === 'undefined' || !value) return;
  const items = storedReactions(userKey);
  const existing = items.find((item) => item.value === value);
  if (existing) {
    existing.count += 1;
    existing.lastUsed = Date.now();
  } else {
    items.push({ value, count: 1, lastUsed: Date.now() });
  }
  try {
    localStorage.setItem(
      storageKey(userKey),
      JSON.stringify(items.sort((left, right) => right.lastUsed - left.lastUsed).slice(0, 24))
    );
  } catch {
    // Private browsing and storage policies can disable persistence. Reactions
    // still work; only the personalized shortcut row is lost.
  }
}
