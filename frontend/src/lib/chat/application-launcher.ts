import type { ApplicationCommand } from './application-commands';
import type { UserApplicationInstallation } from './application-installations';
import {
  applicationInstallPath,
  directoryDetailPath,
  type DirectoryApplicationSummary,
  type DirectoryBotProfileApplication,
  type DirectoryCollection,
  type DirectoryPage
} from './application-directory';
import { parseCanonicalEntityRef } from './refs';

const RECENT_LIMIT = 8;
const RECENT_STORAGE_PREFIX = 'kaede.app-launcher.recents.v1.';

interface LauncherStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

interface RecentCommandIdentity {
  application_ref: string;
  id: string;
  integration_type: ApplicationCommand['integration_type'];
  interaction_context: ApplicationCommand['interaction_context'];
  used_at: number;
}

export interface LauncherCollectionGroup {
  collection: DirectoryCollection | null;
  applications: DirectoryApplicationSummary[];
}

export interface LauncherRecentApplication {
  applicationRef: string;
  applicationName: string;
  command: ApplicationCommand | null;
  installation: UserApplicationInstallation | null;
}

function storageKey(accountRef: string): string | null {
  return parseCanonicalEntityRef(accountRef)
    ? `${RECENT_STORAGE_PREFIX}${encodeURIComponent(accountRef)}`
    : null;
}

function recentIdentity(value: unknown): RecentCommandIdentity | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  if (
    typeof raw.application_ref !== 'string' ||
    !parseCanonicalEntityRef(raw.application_ref) ||
    typeof raw.id !== 'string' ||
    !raw.id ||
    !['guild_install', 'user_install', 'dm_capability'].includes(String(raw.integration_type)) ||
    !['guild', 'bot_dm', 'private_channel'].includes(String(raw.interaction_context)) ||
    typeof raw.used_at !== 'number' ||
    !Number.isSafeInteger(raw.used_at) ||
    raw.used_at < 0
  ) {
    return null;
  }
  return raw as unknown as RecentCommandIdentity;
}

function identityKey(value: Omit<RecentCommandIdentity, 'used_at'>): string {
  return `${value.application_ref}\u0000${value.id}\u0000${value.integration_type}\u0000${value.interaction_context}`;
}

function readRecentEntries(accountRef: string, storage: LauncherStorage): RecentCommandIdentity[] {
  const key = storageKey(accountRef);
  if (!key) return [];
  try {
    const value: unknown = JSON.parse(storage.getItem(key) ?? '[]');
    if (!Array.isArray(value)) return [];
    const seen = new Set<string>();
    const entries: RecentCommandIdentity[] = [];
    for (const item of value) {
      const entry = recentIdentity(item);
      if (!entry) continue;
      const identity = identityKey(entry);
      if (seen.has(identity)) continue;
      seen.add(identity);
      entries.push(entry);
      if (entries.length === RECENT_LIMIT) break;
    }
    return entries;
  } catch {
    return [];
  }
}

function installationCreatedAt(installation: UserApplicationInstallation): number {
  const createdAt = Date.parse(installation.created_at ?? '');
  if (Number.isFinite(createdAt)) return createdAt;
  const updatedAt = Date.parse(installation.updated_at ?? '');
  return Number.isFinite(updatedAt) ? updatedAt : 0;
}

export function activeLauncherInstallations(
  accountRef: string,
  installations: readonly UserApplicationInstallation[]
): UserApplicationInstallation[] {
  if (!parseCanonicalEntityRef(accountRef)) return [];
  const seen = new Set<string>();
  return installations
    .filter((installation) => {
      const application = parseCanonicalEntityRef(installation.application_ref);
      const bot = parseCanonicalEntityRef(installation.bot_user_ref);
      return (
        installation.id.length > 0 &&
        installation.user_ref === accountRef &&
        installation.status === 'active' &&
        installation.revoked_at === null &&
        application !== null &&
        bot !== null &&
        application.origin_domain === bot.origin_domain
      );
    })
    .sort((left, right) => {
      const byCreatedAt = installationCreatedAt(right) - installationCreatedAt(left);
      return byCreatedAt || left.application_ref.localeCompare(right.application_ref);
    })
    .filter((installation) => {
      if (seen.has(installation.application_ref)) return false;
      seen.add(installation.application_ref);
      return true;
    });
}

export function launcherRecentApplications(
  accountRef: string,
  commands: readonly ApplicationCommand[],
  installations: readonly UserApplicationInstallation[],
  storage: LauncherStorage
): LauncherRecentApplication[] {
  const commandsByIdentity = new Map(
    commands.map((command) => [identityKey(command), command] as const)
  );
  const firstCommandByApplication = new Map<string, ApplicationCommand>();
  for (const command of commands) {
    if (!firstCommandByApplication.has(command.application_ref)) {
      firstCommandByApplication.set(command.application_ref, command);
    }
  }
  const activeInstallations = activeLauncherInstallations(accountRef, installations);
  const installationByApplication = new Map(
    activeInstallations.map((installation) => [installation.application_ref, installation] as const)
  );
  const rows: LauncherRecentApplication[] = [];
  const seen = new Set<string>();
  const append = (applicationRef: string, preferredCommand?: ApplicationCommand): void => {
    if (rows.length >= RECENT_LIMIT || seen.has(applicationRef)) return;
    const command = preferredCommand ?? firstCommandByApplication.get(applicationRef) ?? null;
    const installation = installationByApplication.get(applicationRef) ?? null;
    if (!command && !installation) return;
    seen.add(applicationRef);
    rows.push({
      applicationRef,
      applicationName:
        command?.application_name ?? installation?.application_name ?? applicationRef,
      command,
      installation
    });
  };

  for (const recent of readRecentEntries(accountRef, storage)) {
    append(recent.application_ref, commandsByIdentity.get(identityKey(recent)));
  }
  for (const installation of activeInstallations) append(installation.application_ref);
  return rows;
}

export function launcherInstallationDestination(
  installation: UserApplicationInstallation,
  profile: DirectoryBotProfileApplication
): string | null {
  if (
    installation.status !== 'active' ||
    installation.revoked_at !== null ||
    profile.bot_ref !== installation.bot_user_ref ||
    profile.application_ref !== installation.application_ref
  ) {
    return null;
  }
  return profile.directory_listed
    ? directoryDetailPath(profile.application_ref)
    : applicationInstallPath({
        ref: profile.application_ref,
        install_template: profile.install_template
      });
}

export function rememberLauncherCommand(
  accountRef: string,
  command: ApplicationCommand,
  storage: LauncherStorage,
  now = Date.now()
): void {
  const key = storageKey(accountRef);
  if (!key || !Number.isSafeInteger(now) || now < 0) return;
  const next: RecentCommandIdentity = {
    application_ref: command.application_ref,
    id: command.id,
    integration_type: command.integration_type,
    interaction_context: command.interaction_context,
    used_at: now
  };
  const identity = identityKey(next);
  const entries = [
    next,
    ...readRecentEntries(accountRef, storage).filter((entry) => identityKey(entry) !== identity)
  ].slice(0, RECENT_LIMIT);
  try {
    storage.setItem(key, JSON.stringify(entries));
  } catch {
    // Private browsing and quota errors must not block command invocation.
  }
}

export function uninstalledCatalogApplications(
  applications: readonly DirectoryApplicationSummary[],
  commands: readonly ApplicationCommand[]
): DirectoryApplicationSummary[] {
  const installed = new Set(commands.map((command) => command.application_ref));
  const seen = new Set<string>();
  return applications.filter((application) => {
    if (installed.has(application.ref) || seen.has(application.ref)) return false;
    seen.add(application.ref);
    return true;
  });
}

export function launcherCollectionGroups(
  page: Pick<DirectoryPage, 'items' | 'collections'>,
  commands: readonly ApplicationCommand[]
): LauncherCollectionGroup[] {
  const applications = uninstalledCatalogApplications(page.items, commands);
  const assigned = new Set<string>();
  const groups: LauncherCollectionGroup[] = [];
  for (const collection of page.collections) {
    const matches = applications.filter(
      (application) =>
        !assigned.has(application.ref) && application.collections.includes(collection.slug)
    );
    if (!matches.length) continue;
    matches.forEach((application) => assigned.add(application.ref));
    groups.push({ collection, applications: matches });
  }
  const remaining = applications.filter((application) => !assigned.has(application.ref));
  if (remaining.length) groups.push({ collection: null, applications: remaining });
  return groups;
}
