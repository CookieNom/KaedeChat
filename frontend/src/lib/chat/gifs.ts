export interface GifResult {
  id: string;
  title: string;
  url: string;
  preview_url: string;
  width: number | null;
  height: number | null;
}

export interface GifPage {
  items: GifResult[];
  page: number;
  next_page: number | null;
}

const FAVORITES_KEY = 'kaede.gif-favorites.v1';
const MAX_FAVORITES = 100;

export function isGifResult(value: unknown): value is GifResult {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === 'string' &&
    typeof item.title === 'string' &&
    typeof item.url === 'string' &&
    typeof item.preview_url === 'string' &&
    klipyGifUrl(item.url) !== null &&
    klipyGifUrl(item.preview_url) !== null
  );
}

export function loadGifFavorites(): GifResult[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(FAVORITES_KEY) ?? '[]');
    return Array.isArray(stored) ? stored.filter(isGifResult).slice(0, MAX_FAVORITES) : [];
  } catch {
    return [];
  }
}

export function saveGifFavorites(favorites: readonly GifResult[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(favorites.slice(0, MAX_FAVORITES)));
  } catch {
    // Favorites are optional when browser storage is unavailable or full.
  }
}

export function gifFavoriteForUrl(url: string): GifResult {
  return {
    id: url,
    title: 'Saved GIF',
    url,
    preview_url: url,
    width: null,
    height: null
  };
}

export function isGifFavorite(url: string): boolean {
  return loadGifFavorites().some((favorite) => favorite.url === url);
}

export function toggleGifFavorite(gif: GifResult): { favorites: GifResult[]; favorite: boolean } {
  const current = loadGifFavorites();
  const exists = current.some((favorite) => favorite.id === gif.id || favorite.url === gif.url);
  const favorites = exists
    ? current.filter((favorite) => favorite.id !== gif.id && favorite.url !== gif.url)
    : [gif, ...current].slice(0, MAX_FAVORITES);
  saveGifFavorites(favorites);
  return { favorites, favorite: !exists };
}

export function klipyGifUrl(content: string | null): string | null {
  if (!content) return null;
  const candidate = content.trim();
  if (candidate !== content || /\s/.test(candidate)) return null;
  try {
    const url = new URL(candidate);
    if (
      url.protocol !== 'https:' ||
      !['media.klipy.com', 'static.klipy.com'].includes(url.hostname) ||
      url.username ||
      url.password ||
      (url.port && url.port !== '443')
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}
