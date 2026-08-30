import { isCanonicalFederationDomain } from './refs';
import type {
  DirectoryExternalLink,
  DirectoryLocale,
  DirectoryMediaInput
} from './application-directory';

export type ApplicationAssetKind =
  'icon' | 'cover' | 'store' | 'achievement' | 'activity' | 'other';

export interface ApplicationAsset {
  id: string;
  application_ref: string;
  kind: ApplicationAssetKind;
  name: string;
  media_hash: string;
  content_type: string;
  width: number | null;
  height: number | null;
  version: number;
}

export const DIRECTORY_MEDIA_LIMIT = 5;
export const DIRECTORY_EXTERNAL_LINK_LIMIT = 5;
const YOUTUBE_VIDEO_ID = /^[A-Za-z0-9_-]{11}$/u;
const POSITIVE_SNOWFLAKE = /^[1-9][0-9]{0,18}$/u;
const MAX_SNOWFLAKE = 9_223_372_036_854_775_807n;

export const DIRECTORY_LOCALES = [
  ['id', 'Bahasa Indonesia'],
  ['da', 'Dansk'],
  ['de', 'Deutsch'],
  ['en-GB', 'English, UK'],
  ['en-US', 'English, US'],
  ['es-ES', 'Español'],
  ['es-419', 'Español, Latinoamérica'],
  ['fr', 'Français'],
  ['hr', 'Hrvatski'],
  ['it', 'Italiano'],
  ['lt', 'Lietuviškai'],
  ['hu', 'Magyar'],
  ['nl', 'Nederlands'],
  ['no', 'Norsk'],
  ['pl', 'Polski'],
  ['pt-BR', 'Português do Brasil'],
  ['ro', 'Română'],
  ['fi', 'Suomi'],
  ['sv-SE', 'Svenska'],
  ['vi', 'Tiếng Việt'],
  ['tr', 'Türkçe'],
  ['cs', 'Čeština'],
  ['el', 'Ελληνικά'],
  ['bg', 'Български'],
  ['ru', 'Русский'],
  ['uk', 'Українська'],
  ['hi', 'हिन्दी'],
  ['th', 'ไทย'],
  ['zh-CN', '中文（简体）'],
  ['ja', '日本語'],
  ['ko', '한국어'],
  ['zh-TW', '中文（繁體）']
] as const satisfies ReadonlyArray<readonly [DirectoryLocale, string]>;

const DIRECTORY_LOCALE_SET = new Set<DirectoryLocale>(DIRECTORY_LOCALES.map(([locale]) => locale));

export interface DirectorySettingsDraft {
  media: DirectoryMediaInput[];
  externalLinks: DirectoryExternalLink[];
  supportedLocales: DirectoryLocale[];
  descriptionLocalizations: Partial<Record<DirectoryLocale, string>>;
}

export interface DirectorySettingsPayload {
  directory_media: DirectoryMediaInput[];
  directory_external_links: DirectoryExternalLink[];
  directory_supported_locales: DirectoryLocale[];
  directory_description_localizations: Partial<Record<DirectoryLocale, string>>;
}

export function parseYouTubeVideoId(value: string): string | null {
  const candidate = value.trim();
  if (YOUTUBE_VIDEO_ID.test(candidate)) return candidate;

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash) {
    return null;
  }

  let videoId = '';
  const path = url.pathname.split('/').filter(Boolean);
  if (url.hostname === 'youtu.be' && path.length === 1) {
    [videoId] = path;
  } else if (['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(url.hostname)) {
    if (url.pathname === '/watch') videoId = url.searchParams.get('v') ?? '';
    else if (path.length === 2 && (path[0] === 'shorts' || path[0] === 'embed')) {
      videoId = path[1];
    }
  } else if (
    url.hostname === 'www.youtube-nocookie.com' &&
    path.length === 2 &&
    path[0] === 'embed'
  ) {
    videoId = path[1];
  }
  return YOUTUBE_VIDEO_ID.test(videoId) ? videoId : null;
}

export function youtubeEmbedUrl(videoId: string): string | null {
  if (!YOUTUBE_VIDEO_ID.test(videoId)) return null;
  return `https://www.youtube-nocookie.com/embed/${videoId}?rel=0`;
}

export function moveDirectoryItem<T>(items: readonly T[], index: number, offset: -1 | 1): T[] {
  const destination = index + offset;
  if (index < 0 || index >= items.length || destination < 0 || destination >= items.length) {
    return [...items];
  }
  const next = [...items];
  [next[index], next[destination]] = [next[destination], next[index]];
  return next;
}

function validAssetId(value: string): boolean {
  return POSITIVE_SNOWFLAKE.test(value) && BigInt(value) <= MAX_SNOWFLAKE;
}

function normalizedExternalUrl(value: string): string {
  const candidate = value.trim();
  if (!candidate || candidate.length > 2048) {
    throw new Error('Each external link needs an HTTPS URL of at most 2,048 characters.');
  }
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error('Each external link must use a valid HTTPS URL.');
  }
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.hash ||
    !isCanonicalFederationDomain(url.hostname)
  ) {
    throw new Error(
      'External links must use a canonical HTTPS origin without credentials or a fragment.'
    );
  }
  return url.toString();
}

export function directorySettingsPayload(draft: DirectorySettingsDraft): DirectorySettingsPayload {
  if (draft.media.length > DIRECTORY_MEDIA_LIMIT) {
    throw new Error(`Add at most ${DIRECTORY_MEDIA_LIMIT} images or videos.`);
  }
  const mediaKeys = new Set<string>();
  const media = draft.media.map((item): DirectoryMediaInput => {
    const key = item.type === 'image' ? `image:${item.asset_id}` : `youtube:${item.video_id}`;
    if (
      (item.type === 'image' && !validAssetId(item.asset_id)) ||
      (item.type === 'youtube' && !YOUTUBE_VIDEO_ID.test(item.video_id))
    ) {
      throw new Error('Directory media contains an invalid image or YouTube identifier.');
    }
    if (mediaKeys.has(key)) throw new Error('Directory media entries must be unique.');
    mediaKeys.add(key);
    return item.type === 'image'
      ? { type: 'image', asset_id: item.asset_id }
      : { type: 'youtube', video_id: item.video_id };
  });

  if (draft.externalLinks.length > DIRECTORY_EXTERNAL_LINK_LIMIT) {
    throw new Error(`Add at most ${DIRECTORY_EXTERNAL_LINK_LIMIT} external links.`);
  }
  const linkNames = new Set<string>();
  const linkUrls = new Set<string>();
  const externalLinks = draft.externalLinks.map((item): DirectoryExternalLink => {
    const name = item.name.trim();
    if (!name || name.length > 100) {
      throw new Error('Each external link needs a name of at most 100 characters.');
    }
    const url = normalizedExternalUrl(item.url);
    const foldedName = name.toLocaleLowerCase('en-US');
    if (linkNames.has(foldedName) || linkUrls.has(url)) {
      throw new Error('External links must have unique names and URLs.');
    }
    linkNames.add(foldedName);
    linkUrls.add(url);
    return { name, url };
  });

  const supportedLocales = [...draft.supportedLocales];
  if (
    supportedLocales.some((locale) => !DIRECTORY_LOCALE_SET.has(locale)) ||
    new Set(supportedLocales).size !== supportedLocales.length
  ) {
    throw new Error('Supported languages must be valid and unique.');
  }
  supportedLocales.sort();
  const supportedSet = new Set<DirectoryLocale>(supportedLocales);
  const descriptionLocalizations: Partial<Record<DirectoryLocale, string>> = {};
  for (const [rawLocale, rawDescription] of Object.entries(draft.descriptionLocalizations)) {
    const locale = rawLocale as DirectoryLocale;
    const description = rawDescription?.trim() ?? '';
    if (!description) continue;
    if (!DIRECTORY_LOCALE_SET.has(locale) || !supportedSet.has(locale)) {
      throw new Error('Each localized description must use one of the selected languages.');
    }
    if (description.length > 1000) {
      throw new Error('Localized descriptions can contain at most 1,000 characters.');
    }
    descriptionLocalizations[locale] = description;
  }

  return {
    directory_media: media,
    directory_external_links: externalLinks,
    directory_supported_locales: supportedLocales,
    directory_description_localizations: descriptionLocalizations
  };
}

export function syncDirectoryMediaWithAssets(
  media: readonly DirectoryMediaInput[],
  previousAssets: readonly ApplicationAsset[],
  nextAssets: readonly ApplicationAsset[]
): DirectoryMediaInput[] {
  const previousById = new Map(previousAssets.map((asset) => [asset.id, asset]));
  const nextById = new Map(nextAssets.map((asset) => [asset.id, asset]));
  const result = media.filter(
    (item) => item.type !== 'image' || nextById.get(item.asset_id)?.kind === 'store'
  );
  const included = new Set(
    result
      .filter((item) => item.type === 'image')
      .map((item) => (item as { asset_id: string }).asset_id)
  );
  for (const asset of nextAssets) {
    const becameStoreAsset = asset.kind === 'store' && previousById.get(asset.id)?.kind !== 'store';
    if (becameStoreAsset && !included.has(asset.id) && result.length < DIRECTORY_MEDIA_LIMIT) {
      result.push({ type: 'image', asset_id: asset.id });
      included.add(asset.id);
    }
  }
  return result;
}
