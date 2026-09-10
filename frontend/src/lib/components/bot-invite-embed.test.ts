// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import BotInviteEmbed from './BotInviteEmbed.svelte';
import { botInvitesInMessage } from '$lib/chat/bot-invites';

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api: mocks.api,
  userErrorMessage: (_error: unknown, fallback: string) => fallback
}));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
  vi.resetAllMocks();
});

it('previews a remote bot through the local API and keeps authorization on this instance', async () => {
  mocks.api.mockResolvedValue({
    application: {
      id: '123',
      origin_domain: 'apps.example',
      name: 'Weather',
      description: 'Forecasts',
      bot_user: { id: '456', origin_domain: 'apps.example', avatar_hash: 'a'.repeat(64) }
    },
    template: {
      name: 'Community',
      description: null,
      scopes: ['messages.read'],
      e2ee_mode: 'disabled'
    }
  });
  const [reference] = botInvitesInMessage(
    'https://chat.example/applications/123%40apps.example/install/community'
  );
  component = mount(BotInviteEmbed, { target: document.body, props: { reference } });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Weather'));
  expect(mocks.api).toHaveBeenCalledWith('/bot-invites/123%40apps.example/community');
  expect(document.body.textContent).toContain('apps.example');
  expect(document.querySelector('img')?.getAttribute('src')).toContain(
    'https://apps.example/media/assets/'
  );
  expect(decodeURIComponent(document.querySelector('a')!.getAttribute('href')!)).toBe(
    '/applications/123@apps.example/install/community'
  );
});

it('shows an unavailable card when federation cannot resolve the invitation', async () => {
  mocks.api.mockRejectedValue(new Error('Remote unavailable'));
  component = mount(BotInviteEmbed, {
    target: document.body,
    props: { reference: { applicationRef: '123@apps.example', templateSlug: 'community' } }
  });
  await vi.waitFor(() => expect(document.body.textContent).toContain('Bot invitation unavailable'));
  expect(document.querySelector('a')).toBeNull();
});
