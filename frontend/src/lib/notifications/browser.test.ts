import { describe, expect, it } from 'vitest';

import type { Message, UserSummary } from '$lib/chat/types';
import {
  browserNotificationsFromSettings,
  guildNotificationPreferenceKey,
  resolveNotificationPresence,
  shouldNotifyForMessage,
  shouldOfferBrowserNotificationPrompt
} from './browser.svelte';

const currentUser = { id: '1', origin_domain: 'home.test' } as UserSummary;
const message = {
  author_id: '2',
  author_domain: 'remote.test',
  mention_user_refs: []
} as unknown as Message;

describe('browser notification settings', () => {
  it('uses the same normalized key for stored and live guild references', () => {
    expect(
      guildNotificationPreferenceKey({ guild_id: '10', guild_domain: 'Remote.Example ' })
    ).toBe('10@remote.example');
    expect(guildNotificationPreferenceKey({ id: '10', origin_domain: 'remote.example' })).toBe(
      '10@remote.example'
    );
  });

  it('requires an explicit true value', () => {
    expect(browserNotificationsFromSettings({ browser_notifications: true })).toBe(true);
    expect(browserNotificationsFromSettings({ browser_notifications: false })).toBe(false);
    expect(browserNotificationsFromSettings({ browser_notifications: 'true' })).toBe(false);
  });

  it('offers notification opt-in once without attempting to prompt automatically', () => {
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', false, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', true, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', false, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', true, false)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(true, 'denied', false, false)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', false, true)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(false, 'default', false, false)).toBe(false);
  });

  it('notifies for DMs and explicit guild mentions, but never for the sender’s own message', () => {
    expect(shouldNotifyForMessage(message, currentUser, true)).toBe(true);
    expect(shouldNotifyForMessage(message, currentUser, false)).toBe(false);
    expect(
      shouldNotifyForMessage(
        {
          ...message,
          mention_user_refs: [{ id: currentUser.id, origin_domain: currentUser.origin_domain }]
        },
        currentUser,
        false
      )
    ).toBe(true);
    expect(
      shouldNotifyForMessage(
        { ...message, author_id: currentUser.id, author_domain: currentUser.origin_domain },
        currentUser,
        true
      )
    ).toBe(false);
  });

  it('applies the selected guild notification level', () => {
    expect(shouldNotifyForMessage(message, currentUser, false, 'all')).toBe(true);
    expect(shouldNotifyForMessage(message, currentUser, false, 'mentions')).toBe(false);
    expect(shouldNotifyForMessage(message, currentUser, false, 'none')).toBe(false);
    const mention = {
      ...message,
      mention_user_refs: [{ id: currentUser.id, origin_domain: currentUser.origin_domain }]
    };
    expect(shouldNotifyForMessage(mention, currentUser, false, 'mentions')).toBe(true);
    expect(shouldNotifyForMessage(mention, currentUser, false, 'none')).toBe(false);
  });

  it('suppresses every notification while the current user is in do not disturb', () => {
    expect(shouldNotifyForMessage(message, currentUser, true, 'all', 'dnd')).toBe(false);
    expect(
      shouldNotifyForMessage(
        {
          ...message,
          mention_user_refs: [{ id: currentUser.id, origin_domain: currentUser.origin_domain }]
        },
        currentUser,
        false,
        'mentions',
        'dnd'
      )
    ).toBe(false);
  });

  it('uses the locally selected presence immediately instead of waiting for projection sync', () => {
    expect(resolveNotificationPresence('dnd', 'online')).toBe('dnd');
    expect(resolveNotificationPresence('online', 'dnd')).toBe('dnd');
    expect(resolveNotificationPresence('invisible', 'online')).toBe('offline');
    expect(resolveNotificationPresence(null, 'dnd')).toBe('dnd');
    expect(resolveNotificationPresence('invalid', 'idle')).toBe('idle');
  });
});
