import { describe, expect, it } from 'vitest';

import type { Message, UserSummary } from '$lib/chat/types';
import {
  browserNotificationsFromSettings,
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
});
