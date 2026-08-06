const HISTORY_KEY = 'kaede.navigation-history';
const LAST_CHANNEL_KEY = 'kaede.last-channel';
const MAX_HISTORY = 25;

export function readNavigationHistory(storage: Pick<Storage, 'getItem'>): string[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(HISTORY_KEY) ?? '[]');
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string' && item.startsWith('/'))
      : [];
  } catch {
    return [];
  }
}

export function recordNavigation(
  storage: Pick<Storage, 'getItem' | 'setItem'>,
  path: string
): void {
  if (!/^\/(?:g\/|home\/)/.test(path)) return;
  const history = readNavigationHistory(storage).filter((item) => item !== path);
  history.push(path);
  storage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY)));
  storage.setItem(LAST_CHANNEL_KEY, path);
}

export function lastVisitedChannel(storage: Pick<Storage, 'getItem'>): string | null {
  const value = storage.getItem(LAST_CHANNEL_KEY);
  return value && /^\/(?:g\/|home\/)/.test(value) ? value : null;
}
