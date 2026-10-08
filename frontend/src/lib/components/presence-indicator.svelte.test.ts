// @vitest-environment happy-dom
import { expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { chatEntities } from '$lib/stores/entities.svelte';
import PresenceIndicator from './PresenceIndicator.svelte';

it('shows snapshot presence and updates its accessible label when a friend changes status', () => {
  const user = {
    id: '42',
    origin_domain: 'chat.example',
    username: 'cookie',
    display_name: null,
    avatar_hash: null,
    handle: 'cookie@chat.example'
  };
  chatEntities.ingestPresences([
    { user_id: user.id, user_domain: user.origin_domain, status: 'online' }
  ]);
  const target = document.createElement('div');
  const component = mount(PresenceIndicator, { target, props: { user } });
  flushSync();
  expect(target.querySelector('[aria-label="Online"].presence-online')).not.toBeNull();
  flushSync(() => chatEntities.setPresence(user, 'offline'));
  expect(target.querySelector('[aria-label="Offline"].presence-offline')).not.toBeNull();
  void unmount(component);
  chatEntities.clearSession();
});
