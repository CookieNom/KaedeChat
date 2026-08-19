import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Message, UserSummary } from '$lib/chat/types';
import { chatEntities } from '$lib/stores/entities.svelte';
import {
  browserNotificationsConfigured,
  browserNotificationsFromSettings,
  BrowserNotifications,
  guildNotificationPreferenceKey,
  notificationAuthorName,
  resolveNotificationPresence,
  shouldNotifyForMessage,
  shouldOfferBrowserNotificationPrompt
} from './browser.svelte';

afterEach(() => {
  chatEntities.clearSession();
  vi.unstubAllGlobals();
});

const currentUser = { id: '1', origin_domain: 'home.test' } as UserSummary;
const message = {
  author_id: '2',
  author_domain: 'remote.test',
  mention_user_refs: []
} as unknown as Message;

describe('browser notification settings', () => {
  it('never exposes an unresolved local placeholder handle in notification titles', () => {
    expect(
      notificationAuthorName({
        id: '2',
        origin_domain: 'remote.test',
        username: 'history_deadbeef',
        handle: 'history_deadbeef@remote.test',
        display_name: null,
        avatar_hash: null,
        banner_hash: null,
        bio: null,
        custom_status: null,
        profile_version: '1',
        profile_resolved: false
      })
    ).toBe('Remote user · remote.test');
  });

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

  it('distinguishes an erased preference from an explicit opt-out', () => {
    expect(browserNotificationsConfigured({})).toBe(false);
    expect(browserNotificationsConfigured({ browser_notifications: false })).toBe(true);
    expect(browserNotificationsConfigured({ browser_notifications: true })).toBe(true);
  });

  it('offers notification opt-in once without attempting to prompt automatically', () => {
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', false, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', true, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', false, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', true, false)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(true, 'denied', false, false)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(true, 'default', false, true)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(false, 'default', false, false)).toBe(false);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', false, true, false)).toBe(true);
    expect(shouldOfferBrowserNotificationPrompt(true, 'granted', false, true, true)).toBe(false);
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

  it('queues an alert and exposes health guidance when browser permission was revoked', async () => {
    class DeniedNotification {
      static permission: NotificationPermission = 'denied';
    }
    vi.stubGlobal('window', { Notification: DeniedNotification });
    vi.stubGlobal('Notification', DeniedNotification);
    vi.stubGlobal('document', { visibilityState: 'hidden', hasFocus: () => false });
    const notifications = new BrowserNotifications();
    notifications.apply({ browser_notifications: true });
    notifications.applyGuildPreferences([]);

    notifications.notifyMessage({
      ...message,
      id: '20',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test'
    });
    await Promise.resolve();

    expect(notifications.health.pendingCount).toBe(1);
    expect(notifications.health.message).toContain('blocked');
  });

  it('deliberately skips alerts when notifications are disabled without creating a failure', async () => {
    class GrantedNotification {
      static permission: NotificationPermission = 'granted';
    }
    vi.stubGlobal('window', { Notification: GrantedNotification });
    vi.stubGlobal('Notification', GrantedNotification);
    vi.stubGlobal('document', { visibilityState: 'hidden', hasFocus: () => false });
    const notifications = new BrowserNotifications();
    notifications.apply({ browser_notifications: false });
    notifications.applyGuildPreferences([]);

    notifications.notifyMessage({
      ...message,
      id: '21',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test'
    });
    await Promise.resolve();

    expect(notifications.health).toEqual({ message: '', retryable: false, pendingCount: 0 });
  });

  it('pauses delivery after a preference refresh fails instead of using a stale snapshot', async () => {
    class GrantedNotification {
      static permission: NotificationPermission = 'granted';
    }
    vi.stubGlobal('window', { Notification: GrantedNotification });
    vi.stubGlobal('Notification', GrantedNotification);
    vi.stubGlobal('document', { visibilityState: 'hidden', hasFocus: () => false });
    const notifications = new BrowserNotifications();
    notifications.apply({ browser_notifications: false });
    notifications.applyGuildPreferences([]);
    notifications.reportHealthIssue(
      'guild-preferences',
      'Could not refresh guild notification preferences.'
    );

    notifications.notifyMessage({
      ...message,
      id: '22',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test'
    });

    expect(notifications.health.pendingCount).toBe(1);
    notifications.applyGuildPreferences([]);
    await Promise.resolve();
    expect(notifications.health).toEqual({ message: '', retryable: false, pendingCount: 0 });
  });

  it('does not retain own-message or do-not-disturb alerts in the retry queue', async () => {
    let delivered = 0;
    class GrantedNotification {
      static permission: NotificationPermission = 'granted';
      onclick: (() => void) | null = null;

      constructor() {
        delivered += 1;
      }

      close() {}
    }
    vi.stubGlobal('window', { Notification: GrantedNotification });
    vi.stubGlobal('Notification', GrantedNotification);
    vi.stubGlobal('document', { visibilityState: 'hidden', hasFocus: () => false });
    chatEntities.ingestCurrentUser(currentUser);
    chatEntities.channels.upsert({
      id: '10',
      origin_domain: 'home.test',
      guild_id: null,
      guild_domain: null,
      type: 1,
      name: null,
      topic: null,
      position: 0,
      parent_id: null,
      parent_domain: null,
      rate_limit_per_user: 0,
      last_message_id: null,
      last_message_domain: null
    });
    const notifications = new BrowserNotifications();
    notifications.apply({ browser_notifications: true });
    notifications.applyGuildPreferences([]);

    notifications.notifyMessage({
      ...message,
      id: '23',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test',
      author_id: currentUser.id,
      author_domain: currentUser.origin_domain
    });
    await Promise.resolve();
    chatEntities.setPresence(currentUser, 'dnd');
    notifications.notifyMessage({
      ...message,
      id: '24',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test'
    });
    await Promise.resolve();

    expect(delivered).toBe(0);
    expect(notifications.health).toEqual({ message: '', retryable: false, pendingCount: 0 });
  });

  it('does not requeue an old-account native notification after disable', async () => {
    let rejectDelivery!: (reason: unknown) => void;
    const invoke = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectDelivery = reject;
        })
    );
    vi.stubGlobal('window', {
      __TAURI__: { core: { invoke } },
      localStorage: { getItem: () => null },
      location: { origin: 'https://chat.example' }
    });
    vi.stubGlobal('document', { visibilityState: 'hidden', hasFocus: () => false });
    chatEntities.ingestCurrentUser(currentUser);
    chatEntities.channels.upsert({
      id: '10',
      origin_domain: 'home.test',
      guild_id: null,
      guild_domain: null,
      type: 1,
      name: null,
      topic: null,
      position: 0,
      parent_id: null,
      parent_domain: null,
      rate_limit_per_user: 0,
      last_message_id: null,
      last_message_domain: null
    });
    const notifications = new BrowserNotifications();
    notifications.apply({ browser_notifications: true });
    notifications.applyGuildPreferences([]);
    notifications.notifyMessage({
      ...message,
      id: '25',
      origin_domain: 'home.test',
      channel_id: '10',
      channel_domain: 'home.test'
    });
    await Promise.resolve();

    notifications.disable();
    rejectDelivery(new Error('native delivery failed'));
    await Promise.resolve();
    await Promise.resolve();

    expect(notifications.health).toEqual({ message: '', retryable: false, pendingCount: 0 });
  });
});
