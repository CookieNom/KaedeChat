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
