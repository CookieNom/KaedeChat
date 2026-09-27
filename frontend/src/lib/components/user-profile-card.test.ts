// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import UserProfileCard from './UserProfileCard.svelte';
import type { Role, UserSummary } from '$lib/chat/types';

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/client')>()),
  api: mocks.api
}));
const bot: UserSummary = {
  id: '7',
  origin_domain: 'apps.remote',
  username: 'weather',
  handle: 'weather@apps.remote',
  display_name: 'Weather',
  avatar_hash: null,
  account_type: 'bot',
  bio: 'Forecasts for your guild'
};
const role: Role = {
  id: '9',
  origin_domain: 'guild.remote',
  guild_id: '1',
  guild_domain: 'guild.remote',
  name: 'Weather reports',
  color: 0,
  permissions: '0',
  position: 1,
  hoist: false,
  mentionable: false
};
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  vi.resetAllMocks();
});
function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === label || item.getAttribute('aria-label') === label
  );
  expect(found, label).toBeTruthy();
  return found!;
}

it('shares bot profile controls and links to local federated authorization', async () => {
  mocks.api.mockResolvedValue({
    bot_ref: '7@apps.remote',
    application_ref: '8@apps.remote',
    origin_domain: 'apps.remote',
    name: 'Weather',
    directory_listed: false,
    install_template: {
      slug: 'community',
      name: 'Community',
      description: null,
      install_types: ['guild_install'],
      default_install_type: 'guild_install'
    }
  });
  const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
  const onRoleChange = vi.fn().mockResolvedValue(undefined);
  const onMessage = vi.fn();
  const onModerate = vi.fn();
  component = mount(UserProfileCard, {
    target: document.body,
    props: {
      user: bot,
      x: 0,
      y: 0,
      onClose: vi.fn(),
      onMessage,
      roles: [role],
      roleIds: [],
      manageableRoles: [role],
      onRoleChange,
      moderationActions: [{ id: 'kick', label: 'Kick member' }],
      onModerate
    }
  });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Add bot to a guild'));
  expect(document.querySelector('.app-badge')?.textContent).toBe('BOT');
  expect(document.body.textContent).toContain(bot.bio);
  expect(mocks.api).toHaveBeenCalledExactlyOnceWith(
    '/application-directory/bot-profiles/7%40apps.remote',
    { signal: expect.any(AbortSignal) }
  );
  const invite = [...document.querySelectorAll('a')].find((a) =>
    a.textContent?.includes('Add bot')
  )!;
  expect(decodeURIComponent(invite.getAttribute('href')!)).toBe(
    '/applications/8@apps.remote/install/community'
  );
  button('Copy username').click();
  await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('@weather@apps.remote'));
  button('Add role').click();
  await vi.waitFor(() => expect(document.querySelector('.user-role-picker')).not.toBeNull());
  button(role.name).click();
  await vi.waitFor(() => expect(onRoleChange).toHaveBeenCalledWith(bot, role, true));
  button('Message').click();
  expect(onMessage).toHaveBeenCalledWith(bot);
  button('Kick member').click();
  expect(onModerate).toHaveBeenCalledWith(bot, 'kick');
});

it('keeps profiles and role removal usable when installation is unavailable', async () => {
  mocks.api.mockRejectedValue(new Error('Unavailable'));
  const onRoleChange = vi.fn().mockResolvedValue(undefined);
  component = mount(UserProfileCard, {
    target: document.body,
    props: {
      user: bot,
      x: 0,
      y: 0,
      onClose: vi.fn(),
      roles: [role],
      roleIds: [role.id],
      manageableRoles: [role],
      onRoleChange
    }
  });
  await vi.waitFor(() =>
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('Unavailable')
  );
  expect(document.body.textContent).not.toContain('Add bot to a guild');
  button('Remove Weather reports').click();
  await vi.waitFor(() => expect(onRoleChange).toHaveBeenCalledWith(bot, role, false));
  expect(button('Copy username')).toBeTruthy();
});

it('shows the system badge without friendship, app installation, or message actions', async () => {
  mocks.api.mockResolvedValue([]);
  const onMessage = vi.fn();
  component = mount(UserProfileCard, {
    target: document.body,
    props: {
      user: { ...bot, account_type: 'system', display_name: 'Kaede System' },
      x: 0,
      y: 0,
      onClose: vi.fn(),
      onMessage
    }
  });
  await vi.waitFor(() => expect(document.querySelector('.app-badge')?.textContent).toBe('SYSTEM'));
  expect(
    [...document.querySelectorAll('button')].some((item) => item.textContent?.trim() === 'Message')
  ).toBe(false);
  expect(document.querySelector('.relationship-friend')).toBeNull();
  expect(mocks.api.mock.calls.some(([path]) => String(path).includes('/bot-profiles/'))).toBe(
    false
  );
});

const member: UserSummary = {
  ...bot,
  id: String((BigInt(Date.parse('2026-01-14T12:00:00Z')) - 1_767_225_600_000n) << 22n),
  account_type: 'human'
};

it('shows creation and guild join dates without friendship history in guilds', async () => {
  mocks.api.mockResolvedValue([
    { type: 'friend', user: member, updated_at: '2026-03-01T12:00:00Z' }
  ]);
  component = mount(UserProfileCard, {
    target: document.body,
    props: {
      user: member,
      profileContext: 'guild',
      joinedAt: '2026-02-02T12:00:00Z',
      x: 0,
      y: 0,
      onClose: vi.fn()
    }
  });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Friends'));
  expect([...document.querySelectorAll('time')].map((time) => time.dateTime)).toEqual([
    '2026-01-14T12:00:00.000Z',
    '2026-02-02T12:00:00.000Z'
  ]);
  expect(document.body.textContent).toContain('Account created');
  expect(document.body.textContent).toContain('Joined guild');
  expect(document.body.textContent).not.toContain('Friends since');
});

it('uses acceptance time and matches the full user identity in DMs', async () => {
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-03-11T12:00:00Z'));
  mocks.api.mockResolvedValue([
    {
      type: 'friend',
      user: { ...member, origin_domain: 'other.remote' },
      updated_at: '2026-01-01T12:00:00Z'
    },
    {
      type: 'friend',
      user: member,
      created_at: '2026-02-02T12:00:00Z',
      updated_at: '2026-03-01T12:00:00Z'
    }
  ]);
  component = mount(UserProfileCard, {
    target: document.body,
    props: { user: member, x: 0, y: 0, onClose: vi.fn() }
  });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Friends for 10 days'));
  expect([...document.querySelectorAll('time')].map((time) => time.dateTime)).toEqual([
    '2026-01-14T12:00:00.000Z',
    '2026-03-01T12:00:00.000Z'
  ]);
  expect(document.body.textContent).not.toContain('Joined guild');
});

it.each(['pending_in', 'pending_out', 'blocked'])(
  'hides friendship duration for %s',
  async (type) => {
    mocks.api.mockResolvedValue([{ type, user: member, updated_at: '2026-03-01T12:00:00Z' }]);
    component = mount(UserProfileCard, {
      target: document.body,
      props: { user: member, x: 0, y: 0, onClose: vi.fn() }
    });
    await vi.waitFor(() =>
      expect(
        button(
          type === 'pending_in'
            ? 'Accept friend request'
            : type === 'pending_out'
              ? 'Request sent'
              : 'Blocked'
        )
      ).toBeTruthy()
    );
    expect(document.body.textContent).not.toContain('Friends since');
    expect(document.querySelectorAll('time')).toHaveLength(1);
  }
);

it('hides unavailable dates instead of inventing membership history', async () => {
  component = mount(UserProfileCard, {
    target: document.body,
    props: {
      user: { ...member, profile_resolved: false },
      profileContext: 'guild',
      joinedAt: 'invalid',
      x: 0,
      y: 0,
      onClose: vi.fn()
    }
  });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Profile unavailable'));
  expect(document.querySelector('.user-popover-membership')).toBeNull();
});

it('shows friendship history immediately after accepting a request', async () => {
  const acceptedAt = new Date().toISOString();
  mocks.api
    .mockResolvedValueOnce([{ type: 'pending_in', user: member }])
    .mockResolvedValueOnce({ type: 'friend', user: member, updated_at: acceptedAt });
  component = mount(UserProfileCard, {
    target: document.body,
    props: { user: member, x: 0, y: 0, onClose: vi.fn() }
  });
  await vi.waitFor(() => expect(button('Accept friend request')).toBeTruthy());
  button('Accept friend request').click();
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Friends for less than a day')
  );
  expect([...document.querySelectorAll('time')].at(-1)?.dateTime).toBe(acceptedAt);
});
