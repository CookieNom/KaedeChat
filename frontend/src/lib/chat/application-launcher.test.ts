import { describe, expect, it } from 'vitest';
import type { ApplicationCommand } from './application-commands';
import {
  activeLauncherInstallations,
  launcherCollectionGroups,
  launcherInstallationDestination,
  launcherRecentApplications,
  rememberLauncherCommand,
  uninstalledCatalogApplications
} from './application-launcher';
import type {
  DirectoryApplicationSummary,
  DirectoryBotProfileApplication,
  DirectoryPage
} from './application-directory';
import type { UserApplicationInstallation } from './application-installations';

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

const command = (id: string, applicationRef = '2@apps.test'): ApplicationCommand => ({
  id,
  application_ref: applicationRef,
  application_name: applicationRef,
  integration_type: 'guild_install',
  interaction_context: 'guild',
  name: `command-${id}`,
  type: 'chat_input'
});

const application = (
  ref: string,
  collections: DirectoryApplicationSummary['collections'] = []
): DirectoryApplicationSummary => {
  const [id, origin_domain] = ref.split('@');
  return {
    id,
    ref,
    origin_domain,
    name: ref,
    summary: 'A reviewed app',
    category: 'utilities',
    tags: ['utility'],
    collections,
    icon_hash: null,
    banner_hash: null,
    verified: true,
    install_template: {
      slug: 'install',
      name: 'Install',
      description: null,
      install_types: ['guild_install'],
      default_install_type: 'guild_install'
    },
    user_install_supported: false
  };
};

const installation = (
  applicationRef: string,
  createdAt: string | null,
  overrides: Partial<UserApplicationInstallation> = {}
): UserApplicationInstallation => ({
  id: `install-${applicationRef}`,
  application_ref: applicationRef,
  application_name: `Installed ${applicationRef}`,
  application_description: null,
  application_icon_hash: null,
  bot_user_ref: `9@${applicationRef.split('@')[1]}`,
  user_ref: '7@chat.test',
  scopes: ['applications.commands'],
  intents: ['interactions'],
  contexts: ['private_channel'],
  e2ee_participant_capable: false,
  grant_revision: '1',
  status: 'active',
  revoked_at: null,
  created_at: createdAt,
  updated_at: null,
  ...overrides
});

describe('application launcher recents', () => {
  it('keeps command history isolated by qualified account identity', () => {
    const storage = new MemoryStorage();
    const commands = [command('1'), command('2')];
    rememberLauncherCommand('7@chat.test', commands[0], storage, 1);
    rememberLauncherCommand('8@chat.test', commands[1], storage, 2);

    expect(
      launcherRecentApplications('7@chat.test', commands, [], storage).map((row) => row.command)
    ).toEqual([commands[0]]);
    expect(
      launcherRecentApplications('8@chat.test', commands, [], storage).map((row) => row.command)
    ).toEqual([commands[1]]);
    expect(launcherRecentApplications('7@other.test', commands, [], storage)).toEqual([]);
  });

  it('moves a repeated app to the front and keeps one currently available command action', () => {
    const storage = new MemoryStorage();
    const commands = [command('1'), command('2')];
    rememberLauncherCommand('7@chat.test', commands[0], storage, 1);
    rememberLauncherCommand('7@chat.test', commands[1], storage, 2);
    rememberLauncherCommand('7@chat.test', commands[0], storage, 3);

    expect(
      launcherRecentApplications('7@chat.test', commands, [], storage).map((row) => row.command)
    ).toEqual([commands[0]]);
    expect(
      launcherRecentApplications('7@chat.test', [commands[1]], [], storage).map(
        (row) => row.command
      )
    ).toEqual([commands[1]]);
  });

  it('fails closed for malformed account refs and stored data', () => {
    const storage = new MemoryStorage();
    rememberLauncherCommand('not-qualified', command('1'), storage, 1);
    storage.values.set('kaede.app-launcher.recents.v1.7%40chat.test', '{broken');
    expect(launcherRecentApplications('7@chat.test', [command('1')], [], storage)).toEqual([]);
    expect(storage.values.size).toBe(1);
  });

  it('merges recently used apps with newest active account installations', () => {
    const storage = new MemoryStorage();
    const recentCommand = command('1', '2@apps.test');
    const availableInstalledCommand = command('2', '3@apps.test');
    rememberLauncherCommand('7@chat.test', recentCommand, storage, 10);
    const installations = [
      installation('3@apps.test', '2026-01-02T00:00:00Z'),
      installation('4@apps.test', '2026-01-03T00:00:00Z'),
      installation('2@apps.test', '2026-01-01T00:00:00Z')
    ];

    const rows = launcherRecentApplications(
      '7@chat.test',
      [recentCommand, availableInstalledCommand],
      installations,
      storage
    );

    expect(rows.map((row) => row.applicationRef)).toEqual([
      '2@apps.test',
      '4@apps.test',
      '3@apps.test'
    ]);
    expect(rows[0].command).toBe(recentCommand);
    expect(rows[1].command).toBeNull();
    expect(rows[2].command).toBe(availableInstalledCommand);
    expect(rows[0].installation).toBe(installations[2]);
  });

  it('fences installations to the exact active account and deduplicates app refs', () => {
    const newest = installation('3@apps.test', '2026-01-03T00:00:00Z');
    const olderDuplicate = installation('3@apps.test', '2026-01-01T00:00:00Z', {
      id: 'older'
    });
    expect(
      activeLauncherInstallations('7@chat.test', [
        olderDuplicate,
        newest,
        installation('4@apps.test', '2026-01-04T00:00:00Z', {
          user_ref: '8@chat.test'
        }),
        installation('5@apps.test', '2026-01-05T00:00:00Z', { status: 'suspended' }),
        installation('6@apps.test', '2026-01-06T00:00:00Z', {
          revoked_at: '2026-01-07T00:00:00Z'
        }),
        installation('7@apps.test', '2026-01-08T00:00:00Z', {
          bot_user_ref: '9@other.test'
        })
      ])
    ).toEqual([newest]);
    expect(activeLauncherInstallations('not-qualified', [newest])).toEqual([]);
  });

  it('bounds installed app recents to the eight newest applications', () => {
    const storage = new MemoryStorage();
    const installations = Array.from({ length: 10 }, (_, index) =>
      installation(
        `${index + 2}@apps.test`,
        `2026-01-${String(index + 1).padStart(2, '0')}T00:00:00Z`
      )
    );
    expect(
      launcherRecentApplications('7@chat.test', [], installations, storage).map(
        (row) => row.applicationRef
      )
    ).toEqual([
      '11@apps.test',
      '10@apps.test',
      '9@apps.test',
      '8@apps.test',
      '7@apps.test',
      '6@apps.test',
      '5@apps.test',
      '4@apps.test'
    ]);
  });

  it('binds installed app destinations to the attested bot and application', () => {
    const installed = installation('3@apps.test', '2026-01-03T00:00:00Z');
    const profile: DirectoryBotProfileApplication = {
      bot_ref: '9@apps.test',
      application_ref: '3@apps.test',
      origin_domain: 'apps.test',
      name: 'Tasks',
      install_template: {
        slug: 'review',
        name: 'Review',
        description: null,
        install_types: ['user_install'],
        default_install_type: 'user_install'
      },
      directory_listed: true
    };

    expect(launcherInstallationDestination(installed, profile)).toBe(
      '/application-directory/3%40apps.test'
    );
    expect(
      launcherInstallationDestination(installed, { ...profile, directory_listed: false })
    ).toBe('/applications/3%40apps.test/install/review');
    expect(
      launcherInstallationDestination(installed, {
        ...profile,
        application_ref: '4@apps.test'
      })
    ).toBeNull();
    expect(
      launcherInstallationDestination(installed, { ...profile, bot_ref: '8@apps.test' })
    ).toBeNull();
  });
});

describe('application launcher catalog', () => {
  it('deduplicates reviewed apps and excludes apps already installed in this context', () => {
    const first = application('2@apps.test');
    const second = application('3@apps.test');
    expect(uninstalledCatalogApplications([first, first, second], [command('1')])).toEqual([
      second
    ]);
  });

  it('projects displayed collections and a fallback explore group without duplicate cards', () => {
    const featured = application('3@apps.test', ['featured']);
    const unassigned = application('4@apps.test');
    const page = {
      items: [featured, unassigned],
      collections: [
        { slug: 'featured', name: 'Featured', description: 'Featured apps' },
        { slug: 'staff-picks', name: 'Staff Picks', description: 'Staff picks' }
      ]
    } satisfies Pick<DirectoryPage, 'items' | 'collections'>;

    expect(launcherCollectionGroups(page, [])).toEqual([
      { collection: page.collections[0], applications: [featured] },
      { collection: null, applications: [unassigned] }
    ]);
  });
});
