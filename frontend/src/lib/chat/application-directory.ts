import { entityRef, isCanonicalFederationDomain, parseCanonicalEntityRef } from './refs';

export interface DirectoryInstallTemplate {
  slug: string;
  name: string;
  description: string | null;
  install_types: Array<'guild_install' | 'user_install'>;
  default_install_type: 'guild_install' | 'user_install';
}

export interface DirectoryBotProfileApplication {
  bot_ref: string;
  application_ref: string;
  origin_domain: string;
  name: string;
  install_template: DirectoryInstallTemplate;
  directory_listed: boolean;
}

function exactRecord(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
    ? record
    : null;
}

function parseDirectoryInstallTemplate(value: unknown): DirectoryInstallTemplate | null {
  const record = exactRecord(value, [
    'slug',
    'name',
    'description',
    'install_types',
    'default_install_type'
  ]);
  if (!record) return null;
  const installTypes = record.install_types;
  if (
    typeof record.slug !== 'string' ||
    !/^[a-z0-9][a-z0-9_-]{1,63}$/u.test(record.slug) ||
    typeof record.name !== 'string' ||
    record.name.length < 1 ||
    record.name.length > 100 ||
    (record.description !== null &&
      (typeof record.description !== 'string' || record.description.length > 500)) ||
    !Array.isArray(installTypes) ||
    installTypes.length < 1 ||
    installTypes.length > 2 ||
    installTypes.some((item) => item !== 'guild_install' && item !== 'user_install') ||
    new Set(installTypes).size !== installTypes.length ||
    (record.default_install_type !== 'guild_install' &&
      record.default_install_type !== 'user_install') ||
    !installTypes.includes(record.default_install_type)
  ) {
    return null;
  }
  return {
    slug: record.slug,
    name: record.name,
    description: record.description as string | null,
    install_types: installTypes as Array<'guild_install' | 'user_install'>,
    default_install_type: record.default_install_type
  };
}

export function parseDirectoryBotProfileApplication(
  value: unknown,
  expectedBotRef: string
): DirectoryBotProfileApplication | null {
  const record = exactRecord(value, [
    'bot_ref',
    'application_ref',
    'origin_domain',
    'name',
    'install_template',
    'directory_listed'
  ]);
  if (
    !record ||
    typeof record.bot_ref !== 'string' ||
    record.bot_ref !== expectedBotRef ||
    typeof record.application_ref !== 'string' ||
    typeof record.origin_domain !== 'string' ||
    typeof record.name !== 'string' ||
    record.name.length < 1 ||
    record.name.length > 100 ||
    typeof record.directory_listed !== 'boolean'
  ) {
    return null;
  }
  const bot = parseCanonicalEntityRef(record.bot_ref, record.origin_domain);
  const application = parseCanonicalEntityRef(record.application_ref, record.origin_domain);
  const installTemplate = parseDirectoryInstallTemplate(record.install_template);
  if (!bot || !application || !installTemplate) return null;
  return {
    bot_ref: record.bot_ref,
    application_ref: record.application_ref,
    origin_domain: record.origin_domain,
    name: record.name,
    install_template: installTemplate,
    directory_listed: record.directory_listed
  };
}

export interface DirectoryApplicationSummary {
  id: string;
  ref: string;
  origin_domain: string;
  name: string;
  summary: string;
  category: DirectoryCategory;
  tags: string[];
  collections: DirectoryCollectionSlug[];
  icon_hash: string | null;
  banner_hash: string | null;
  verified: boolean;
  install_template: DirectoryInstallTemplate;
  user_install_supported: boolean;
}

export interface DirectoryImageMediaInput {
  type: 'image';
  asset_id: string;
}

export interface DirectoryImageMedia extends DirectoryImageMediaInput {
  name: string;
  media_hash: string;
  content_type: string;
  width: number | null;
  height: number | null;
}

export interface DirectoryYouTubeMedia {
  type: 'youtube';
  video_id: string;
}

export type DirectoryMediaInput = DirectoryImageMediaInput | DirectoryYouTubeMedia;
export type DirectoryMedia = DirectoryImageMedia | DirectoryYouTubeMedia;

export interface DirectoryExternalLink {
  name: string;
  url: string;
}

export interface DirectoryPopularCommand {
  id: string;
  name: string;
  description: string;
}

export interface DirectorySimilarApplication {
  id: string;
  ref: string;
  origin_domain: string;
  name: string;
  summary: string;
  category: DirectoryCategory;
  tags: string[];
  icon_hash: string | null;
}

export interface DirectoryApplicationDetail extends DirectoryApplicationSummary {
  description: string;
  support_url: string;
  privacy_policy_url: string;
  terms_url: string;
  media: DirectoryMedia[];
  external_links: DirectoryExternalLink[];
  supported_locales: DirectoryLocale[];
  description_localizations: Partial<Record<DirectoryLocale, string>>;
  popular_commands: DirectoryPopularCommand[];
  similar_apps: DirectorySimilarApplication[];
}

export type DirectoryReadinessKey =
  | 'directory_enabled'
  | 'summary'
  | 'category'
  | 'tags'
  | 'description'
  | 'support_url'
  | 'privacy_url'
  | 'terms_url'
  | 'media'
  | 'external_links'
  | 'supported_locales'
  | 'description_localizations'
  | 'install_path'
  | 'user_install_command';

export interface DirectoryReadiness {
  status: 'incomplete' | 'ready_for_review' | 'approved';
  ready: boolean;
  preview_available: boolean;
  missing: DirectoryReadinessKey[];
  items: Array<{ key: DirectoryReadinessKey; ready: boolean }>;
}

export interface DirectoryPreviewApplication {
  id: string;
  ref: string;
  origin_domain: string;
  name: string;
  summary: string | null;
  category: DirectoryCategory | null;
  tags: string[];
  collections: DirectoryCollectionSlug[];
  icon_hash: string | null;
  banner_hash: string | null;
  verified: boolean;
  install_template: DirectoryInstallTemplate | null;
  user_install_supported: boolean;
  description: string | null;
  support_url: string | null;
  privacy_policy_url: string | null;
  terms_url: string | null;
  media: DirectoryMedia[];
  external_links: DirectoryExternalLink[];
  supported_locales: DirectoryLocale[];
  description_localizations: Partial<Record<DirectoryLocale, string>>;
  popular_commands: DirectoryPopularCommand[];
  similar_apps: DirectorySimilarApplication[];
}

export interface DirectoryPreviewResponse {
  application_ref: string;
  application: DirectoryPreviewApplication;
  readiness: DirectoryReadiness;
}

export type DirectoryCollectionSlug = 'featured' | 'staff-picks' | 'new-and-noteworthy';

export type DirectoryCategory =
  'entertainment' | 'games' | 'moderation' | 'productivity' | 'social' | 'utilities';

export type DirectoryLocale =
  | 'id'
  | 'da'
  | 'de'
  | 'en-GB'
  | 'en-US'
  | 'es-ES'
  | 'es-419'
  | 'fr'
  | 'hr'
  | 'it'
  | 'lt'
  | 'hu'
  | 'nl'
  | 'no'
  | 'pl'
  | 'pt-BR'
  | 'ro'
  | 'fi'
  | 'sv-SE'
  | 'vi'
  | 'tr'
  | 'cs'
  | 'el'
  | 'bg'
  | 'ru'
  | 'uk'
  | 'hi'
  | 'th'
  | 'zh-CN'
  | 'ja'
  | 'ko'
  | 'zh-TW';

export interface DirectoryFilters {
  query: string;
  category: DirectoryCategory | '';
  domain: string;
  collection: DirectoryCollectionSlug | '';
}

export interface DirectoryCollection {
  slug: DirectoryCollectionSlug;
  name: string;
  description: string;
}

export interface DirectoryPage {
  items: DirectoryApplicationSummary[];
  next_cursor: string | null;
  collections: DirectoryCollection[];
  selected_collection: DirectoryCollectionSlug | null;
}

const DIRECTORY_CATEGORIES = new Set<DirectoryCategory>([
  'entertainment',
  'games',
  'moderation',
  'productivity',
  'social',
  'utilities'
]);
const DIRECTORY_COLLECTIONS = new Set<DirectoryCollectionSlug>([
  'featured',
  'staff-picks',
  'new-and-noteworthy'
]);
const DIRECTORY_LIST_PATH = '/application-directory';
const MAX_RESTORED_PAGES = 10;

export const EMPTY_DIRECTORY_FILTERS: Readonly<DirectoryFilters> = {
  query: '',
  category: '',
  domain: '',
  collection: ''
};

export function canonicalDirectoryDomain(value: string): string {
  const normalized = value.trim().toLowerCase().replace(/\.+$/, '');
  return isCanonicalFederationDomain(normalized) ? normalized : '';
}

export function canonicalDirectoryFilters(filters: DirectoryFilters): DirectoryFilters {
  return {
    query: filters.query.trim().slice(0, 100),
    category: DIRECTORY_CATEGORIES.has(filters.category as DirectoryCategory)
      ? (filters.category as DirectoryCategory)
      : '',
    domain: canonicalDirectoryDomain(filters.domain),
    collection: DIRECTORY_COLLECTIONS.has(filters.collection as DirectoryCollectionSlug)
      ? (filters.collection as DirectoryCollectionSlug)
      : ''
  };
}

export function directoryFiltersFromSearchParams(params: URLSearchParams): DirectoryFilters {
  return canonicalDirectoryFilters({
    query: params.get('q') ?? '',
    category: (params.get('category') ?? '') as DirectoryCategory | '',
    domain: params.get('domain') ?? '',
    collection: (params.get('collection') ?? '') as DirectoryCollectionSlug | ''
  });
}

export function directoryRestoredPageCount(params: URLSearchParams): number {
  const value = Number(params.get('pages') ?? '1');
  return Number.isSafeInteger(value) && value >= 1 ? Math.min(value, MAX_RESTORED_PAGES) : 1;
}

function appendDirectoryFilters(params: URLSearchParams, filters: DirectoryFilters): void {
  const canonical = canonicalDirectoryFilters(filters);
  if (canonical.query) params.set('q', canonical.query);
  if (canonical.category) params.set('category', canonical.category);
  if (canonical.domain) params.set('domain', canonical.domain);
  if (canonical.collection) params.set('collection', canonical.collection);
}

export function directoryQuery(filters: DirectoryFilters, after?: string): string {
  const params = new URLSearchParams();
  appendDirectoryFilters(params, filters);
  params.set('limit', '24');
  if (after) params.set('after', after);
  return `/application-directory?${params.toString()}`;
}

export function directoryPagePath(
  filters: DirectoryFilters = EMPTY_DIRECTORY_FILTERS,
  from: string | null = null,
  pages = 1
): string {
  const params = new URLSearchParams();
  appendDirectoryFilters(params, filters);
  if (from) params.set('from', from);
  if (pages > 1) params.set('pages', String(Math.min(Math.floor(pages), MAX_RESTORED_PAGES)));
  const query = params.toString();
  return query ? `${DIRECTORY_LIST_PATH}?${query}` : DIRECTORY_LIST_PATH;
}

export function directoryEntryPath(from: string): string {
  return directoryPagePath(EMPTY_DIRECTORY_FILTERS, from);
}

export function directoryDetailPath(applicationRef: string, returnTo?: string): string {
  const path = `${DIRECTORY_LIST_PATH}/${encodeURIComponent(applicationRef)}`;
  if (!returnTo) return path;
  const params = new URLSearchParams({ return_to: returnTo });
  return `${path}?${params.toString()}`;
}

function safeSameOriginPath(value: string | null, origin: string): URL | null {
  if (!value || !value.startsWith('/')) return null;
  try {
    const base = new URL(origin);
    const target = new URL(value, base);
    return target.origin === base.origin ? target : null;
  } catch {
    return null;
  }
}

export function safeDirectoryListReturnPath(value: string | null, origin: string): string | null {
  const target = safeSameOriginPath(value, origin);
  if (!target || target.pathname !== DIRECTORY_LIST_PATH) return null;
  return `${target.pathname}${target.search}${target.hash}`;
}

export function safeDirectoryApplicationReturnPath(
  value: string | null,
  origin: string
): string | null {
  const target = safeSameOriginPath(value, origin);
  const prefix = `${DIRECTORY_LIST_PATH}/`;
  if (!target || !target.pathname.startsWith(prefix)) return null;

  const encodedApplicationRef = target.pathname.slice(prefix.length);
  if (!encodedApplicationRef || encodedApplicationRef.includes('/')) return null;
  let applicationRef: string;
  try {
    applicationRef = decodeURIComponent(encodedApplicationRef);
  } catch {
    return null;
  }
  const parsed = parseCanonicalEntityRef(applicationRef);
  if (!parsed) return null;

  const canonicalPath = `${prefix}${encodeURIComponent(entityRef(parsed))}`;
  return `${canonicalPath}${target.search}${target.hash}`;
}

export function applicationInstallPath(
  application: Pick<DirectoryApplicationSummary, 'ref' | 'install_template'>,
  returnTo?: string
): string | null {
  if (!application.install_template) return null;
  const path = `/applications/${encodeURIComponent(application.ref)}/install/${encodeURIComponent(application.install_template.slug)}`;
  if (!returnTo) return path;
  const params = new URLSearchParams({ return_to: returnTo });
  return `${path}?${params.toString()}`;
}
