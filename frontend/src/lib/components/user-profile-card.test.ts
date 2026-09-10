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
