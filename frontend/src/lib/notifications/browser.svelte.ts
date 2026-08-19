import { userErrorMessage } from '$lib/api/client';
import { entityKey } from '$lib/chat/refs';
import type { Message, PresenceStatus, UserSummary } from '$lib/chat/types';
import { userDisplayName } from '$lib/chat/users';
import { assetUrl } from '$lib/media/assets';
import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
import { chatEntities } from '$lib/stores/entities.svelte';
import { isNativeDesktop, nativeError, nativeInvoke } from '$lib/platform/native';
import { SvelteMap, SvelteSet, SvelteURL } from 'svelte/reactivity';

export type GuildNotificationLevel = 'all' | 'mentions' | 'none';

export interface GuildNotificationPreference {
  guild_id: string;
  guild_domain: string;
  level: GuildNotificationLevel;
}

export interface NotificationHealth {
  message: string;
  retryable: boolean;
  pendingCount: number;
}

export function guildNotificationPreferenceKey(
  guild: { id: string; origin_domain: string } | { guild_id: string; guild_domain: string }
): string {
  const id = 'id' in guild ? guild.id : guild.guild_id;
  const domain = 'origin_domain' in guild ? guild.origin_domain : guild.guild_domain;
  return `${id}@${domain.trim().toLowerCase()}`;
}

export function browserNotificationsFromSettings(settings: Record<string, unknown>): boolean {
  return settings.browser_notifications === true;
}

export function browserNotificationsConfigured(settings: Record<string, unknown>): boolean {
  return typeof settings.browser_notifications === 'boolean';
}

export function resolveNotificationPresence(
  storedPreference: string | null,
  projectedPresence: PresenceStatus
): PresenceStatus {
  if (storedPreference === 'dnd' || projectedPresence === 'dnd') return 'dnd';
  if (storedPreference === 'online' || storedPreference === 'idle') {
    return storedPreference;
  }
  if (storedPreference === 'invisible') return 'offline';
  return projectedPresence;
}

export function shouldOfferBrowserNotificationPrompt(
  supported: boolean,
  permission: NotificationPermission,
  enabled: boolean,
  promptHandled: boolean,
  configured = true
): boolean {
  if (!supported || permission === 'denied') return false;
  // Account data can be restored or reset independently of browser storage.
  // In that case an old local "handled" flag must not hide the only route to
  // recreate the missing account-level notification preference.
  if (!configured) return true;
  if (promptHandled) return false;
  return permission === 'default' || !enabled;
}

export function shouldNotifyForMessage(
  message: Message,
  currentUser: UserSummary,
  isDirectMessage: boolean,
  guildLevel: GuildNotificationLevel = 'mentions',
  currentPresence: PresenceStatus = 'online'
): boolean {
  if (currentPresence === 'dnd') return false;
  if (message.author_id === currentUser.id && message.author_domain === currentUser.origin_domain) {
    return false;
  }
  if (isDirectMessage) return true;
  if (guildLevel === 'none') return false;
  if (guildLevel === 'all') return true;
  const mentions = Array.isArray(message.mention_user_refs) ? message.mention_user_refs : [];
  return mentions.some(
    (reference) =>
      reference.id === currentUser.id && reference.origin_domain === currentUser.origin_domain
  );
}

export function notificationAuthorName(author: UserSummary | null | undefined): string {
  return author ? userDisplayName(author) : 'Someone';
}

export class BrowserNotifications {
  enabled = $state(false);
  configured = $state(false);
  permission = $state<NotificationPermission>('default');
  permissionError = $state('');
  promptHandled = $state(false);
  health = $state<NotificationHealth>({ message: '', retryable: false, pendingCount: 0 });
  #settingsLoaded = false;
  #guildPreferencesLoaded = false;
  #guildLevels = new SvelteMap<string, GuildNotificationLevel>();
  #pendingMessages = new SvelteMap<string, Message>();
  #deliveryInFlight = new SvelteSet<string>();
  #deliveryFailures = new SvelteSet<string>();
  #healthIssues = new SvelteMap<string, string>();
  #generation = 0;

  get supported(): boolean {
    return isNativeDesktop() || (typeof window !== 'undefined' && 'Notification' in window);
  }

  apply(settings: Record<string, unknown>): void {
    this.configured = browserNotificationsConfigured(settings);
    this.enabled = browserNotificationsFromSettings(settings);
    this.#settingsLoaded = true;
    this.clearHealthIssue('settings');
    this.refreshPermission();
    this.#flushPending();
  }

  applyGuildPreferences(preferences: GuildNotificationPreference[]): void {
    this.#guildLevels = new SvelteMap(
      preferences.map((preference) => [
        guildNotificationPreferenceKey(preference),
        preference.level
      ])
    );
    this.#guildPreferencesLoaded = true;
    this.clearHealthIssue('guild-preferences');
    this.#flushPending();
  }

  setGuildPreference(
    guild: { id: string; origin_domain: string },
    level: GuildNotificationLevel
  ): void {
    this.#guildLevels.set(guildNotificationPreferenceKey(guild), level);
    this.#flushPending();
  }

  refreshPermission(): void {
    this.permission = isNativeDesktop()
      ? 'granted'
      : this.supported
        ? Notification.permission
        : 'denied';
  }

  refreshPromptPreference(): void {
    if (typeof window === 'undefined') return;
    try {
      this.promptHandled = window.localStorage.getItem('kaede:notification-prompt') === 'handled';
    } catch {
      this.promptHandled = false;
    }
  }

  markPromptHandled(): void {
    this.promptHandled = true;
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem('kaede:notification-prompt', 'handled');
    } catch {
      // Private browsing modes can make localStorage unavailable. The in-memory
      // flag still prevents the prompt from repeating during this page load.
    }
  }

  async requestPermission(): Promise<NotificationPermission> {
    this.permissionError = '';
    if (isNativeDesktop()) {
      try {
        await nativeInvoke('native_notifications_prepare');
        this.permission = 'granted';
      } catch (caught) {
        this.permissionError = userErrorMessage(
          nativeError(caught),
          'Could not enable desktop notifications. Check system notification settings and try again.'
        );
        this.permission = 'denied';
      }
      return this.permission;
    }
    if (!this.supported) {
      this.permission = 'denied';
      return this.permission;
    }
    try {
      this.permission = await Notification.requestPermission();
    } catch (caught) {
      this.permissionError = userErrorMessage(
        caught,
        'The browser could not request notification permission. Check site settings and try again.'
      );
      this.permission = 'denied';
    }
    return this.permission;
  }

  disable(): void {
    this.#generation += 1;
    this.enabled = false;
    this.configured = false;
    this.promptHandled = false;
    this.#settingsLoaded = false;
    this.#guildPreferencesLoaded = false;
    this.#guildLevels.clear();
    this.#pendingMessages.clear();
    this.#deliveryInFlight.clear();
    this.#deliveryFailures.clear();
    this.#healthIssues.clear();
    this.#syncHealth();
  }

  notifyMessage(message: Message): void {
    if (document.visibilityState === 'visible' && document.hasFocus()) return;
    if (!this.#settingsLoaded || !this.#guildPreferencesLoaded) {
      this.#queuePending(message);
      return;
    }
    void this.#attemptDelivery(message, this.#generation);
  }

  reportHealthIssue(key: 'settings' | 'guild-preferences', message: string): void {
    if (key === 'settings') this.#settingsLoaded = false;
    if (key === 'guild-preferences') this.#guildPreferencesLoaded = false;
    this.#healthIssues.set(key, message);
    this.#syncHealth();
  }

  clearHealthIssue(key: 'settings' | 'guild-preferences'): void {
    this.#healthIssues.delete(key);
    this.#syncHealth();
  }

  retryPending(): void {
    this.refreshPermission();
    this.#healthIssues.delete('queue');
    this.#flushPending();
  }

  async #attemptDelivery(message: Message, generation: number): Promise<void> {
    if (generation !== this.#generation) return;
    const key = entityKey(message);
    const inFlightKey = `${generation}:${key}`;
    if (this.#deliveryInFlight.has(inFlightKey)) return;
    this.#deliveryInFlight.add(inFlightKey);
    try {
      const delivered = await this.#deliver(message, generation);
      if (generation !== this.#generation) return;
      if (delivered) {
        this.#pendingMessages.delete(key);
        this.#deliveryFailures.delete(key);
        if (!this.#deliveryFailures.size) this.#healthIssues.delete('delivery');
      } else {
        this.#queuePending(message);
      }
    } finally {
      this.#deliveryInFlight.delete(inFlightKey);
      this.#syncHealth();
    }
  }

  async #deliver(message: Message, generation: number): Promise<boolean> {
    if (!this.enabled) return true;
    if (!this.supported) {
      this.#deliveryFailed(
        message,
        'This browser cannot show system notifications. Use a supported browser or the Kaede desktop app.'
      );
      return false;
    }
    if (!isNativeDesktop() && Notification.permission !== 'granted') {
      this.refreshPermission();
      this.#deliveryFailed(
        message,
        this.permission === 'denied'
          ? 'Browser notifications are blocked. Allow them in this site’s settings, then retry.'
          : 'Browser notification permission is required. Retry and allow notifications when prompted.'
      );
      return false;
    }

    const currentUser = chatEntities.currentUser;
    if (!currentUser) {
      this.#healthIssues.set(
        'entities',
        'Notification details are still loading. Kaede is holding the alert until they are available.'
      );
      this.#syncHealth();
      return false;
    }
    const projectedPresence = chatEntities.presenceFor(currentUser);
    let storedPresence: string | null = null;
    try {
      storedPresence = window.localStorage.getItem('kaede.presence');
    } catch {
      // The live presence projection remains authoritative when storage is unavailable.
    }
    const currentPresence = resolveNotificationPresence(storedPresence, projectedPresence);
    const channel = chatEntities.channels.get(`${message.channel_id}@${message.channel_domain}`);
    if (!channel) {
      this.#healthIssues.set(
        'entities',
        'Notification details are still loading. Kaede is holding the alert until they are available.'
      );
      this.#syncHealth();
      return false;
    }
    this.#healthIssues.delete('entities');
    const isDirectMessage = channel.guild_id === null;
    const guildLevel =
      channel.guild_id && channel.guild_domain
        ? (this.#guildLevels.get(
            guildNotificationPreferenceKey({
              guild_id: channel.guild_id,
              guild_domain: channel.guild_domain
            })
          ) ?? 'mentions')
        : 'mentions';
    if (!shouldNotifyForMessage(message, currentUser, isDirectMessage, guildLevel, currentPresence))
      return true;

    const author =
      message.author ?? chatEntities.users.get(`${message.author_id}@${message.author_domain}`);
    const authorName = notificationAuthorName(author);
    const guild =
      channel.guild_id && channel.guild_domain
        ? chatEntities.guilds.get(`${channel.guild_id}@${channel.guild_domain}`)
        : undefined;
    const title = isDirectMessage
      ? authorName
      : `${authorName} in #${channel.name ?? 'channel'}${guild ? ` · ${guild.name}` : ''}`;
    const body = message.content?.trim().slice(0, 180) || 'Sent an attachment';
    const icon = author?.avatar_hash
      ? new SvelteURL(assetUrl(author.avatar_hash, 'thumbnail_128', author), window.location.origin)
          .href
      : undefined;
    if (isNativeDesktop()) {
      try {
        await nativeInvoke('native_notify', {
          title,
          body,
          sensitive: false,
          deepLink: guild ? guildChannelPath(guild, channel) : directMessagePath(channel)
        });
        return true;
      } catch (caught) {
        if (generation === this.#generation) {
          this.#deliveryFailed(
            message,
            userErrorMessage(
              nativeError(caught),
              'Desktop notification delivery failed. Check system notification settings and try again.'
            )
          );
        }
        return false;
      }
    }
    try {
      const notification = new Notification(title, {
        body,
        icon,
        tag: `message:${entityKey(message)}`
      });
      notification.onclick = () => {
        window.focus();
        const destination = guild ? guildChannelPath(guild, channel) : directMessagePath(channel);
        window.location.assign(destination);
        notification.close();
      };
    } catch (caught) {
      // Notification permission can be revoked between the permission check and construction.
      this.refreshPermission();
      this.#deliveryFailed(
        message,
        this.permission === 'denied'
          ? 'Browser notifications are now blocked. Allow them in this site’s settings, then retry.'
          : userErrorMessage(
              caught,
              'The browser could not show a notification. Check browser and system notification settings, then retry.'
            )
      );
      return false;
    }
    return true;
  }

  #deliveryFailed(message: Message, detail: string): void {
    this.#deliveryFailures.add(entityKey(message));
    this.#healthIssues.set('delivery', detail);
    this.#syncHealth();
  }

  #queuePending(message: Message): void {
    this.#pendingMessages.set(entityKey(message), message);
    if (this.#pendingMessages.size > 50) {
      const oldest = this.#pendingMessages.keys().next().value as string | undefined;
      if (oldest) {
        this.#pendingMessages.delete(oldest);
        this.#deliveryFailures.delete(oldest);
        this.#healthIssues.set(
          'queue',
          'Some system notifications could not be queued. Review Kaede’s unread badges for messages you may have missed.'
        );
      }
    }
    this.#syncHealth();
  }

  #flushPending(): void {
    if (!this.#settingsLoaded || !this.#guildPreferencesLoaded) return;
    const appIsActive = document.visibilityState === 'visible' && document.hasFocus();
    for (const [key, message] of this.#pendingMessages) {
      if (appIsActive) {
        this.#pendingMessages.delete(key);
        this.#deliveryFailures.delete(key);
      } else {
        void this.#attemptDelivery(message, this.#generation);
      }
    }
    if (!this.#deliveryFailures.size) this.#healthIssues.delete('delivery');
    this.#syncHealth();
  }

  #syncHealth(): void {
    const message = this.#healthIssues.values().next().value ?? '';
    this.health = {
      message,
      retryable: this.#healthIssues.size > 0,
      pendingCount: this.#pendingMessages.size
    };
  }
}

export const browserNotifications = new BrowserNotifications();
