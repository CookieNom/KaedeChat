import { entityKey } from '$lib/chat/refs';
import type { Message, PresenceStatus, UserSummary } from '$lib/chat/types';
import { assetUrl } from '$lib/media/assets';
import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
import { chatEntities } from '$lib/stores/entities.svelte';
import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';

export type GuildNotificationLevel = 'all' | 'mentions' | 'none';

export interface GuildNotificationPreference {
  guild_id: string;
  guild_domain: string;
  level: GuildNotificationLevel;
}

export function browserNotificationsFromSettings(settings: Record<string, unknown>): boolean {
  return settings.browser_notifications === true;
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
  promptHandled: boolean
): boolean {
  if (!supported || permission === 'denied' || promptHandled) return false;
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
  return message.mention_user_refs.some(
    (reference) =>
      reference.id === currentUser.id && reference.origin_domain === currentUser.origin_domain
  );
}

class BrowserNotifications {
  enabled = $state(false);
  permission = $state<NotificationPermission>('default');
  promptHandled = $state(false);
  #guildPreferencesLoaded = false;
  #guildLevels = new Map<string, GuildNotificationLevel>();

  get supported(): boolean {
    return isNativeDesktop() || (typeof window !== 'undefined' && 'Notification' in window);
  }

  apply(settings: Record<string, unknown>): void {
    this.enabled = browserNotificationsFromSettings(settings);
    this.refreshPermission();
  }

  applyGuildPreferences(preferences: GuildNotificationPreference[]): void {
    this.#guildLevels = new Map(
      preferences.map((preference) => [
        `${preference.guild_id}@${preference.guild_domain}`,
        preference.level
      ])
    );
    this.#guildPreferencesLoaded = true;
  }

  setGuildPreference(
    guild: { id: string; origin_domain: string },
    level: GuildNotificationLevel
  ): void {
    this.#guildLevels.set(`${guild.id}@${guild.origin_domain}`, level);
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
    if (isNativeDesktop()) {
      this.permission = 'granted';
      return this.permission;
    }
    if (!this.supported) {
      this.permission = 'denied';
      return this.permission;
    }
    this.permission = await Notification.requestPermission();
    return this.permission;
  }

  disable(): void {
    this.enabled = false;
    this.promptHandled = false;
    this.#guildPreferencesLoaded = false;
    this.#guildLevels.clear();
  }

  notifyMessage(message: Message): void {
    if (
      !this.enabled ||
      !this.supported ||
      (!isNativeDesktop() && Notification.permission !== 'granted')
    )
      return;
    if (document.visibilityState === 'visible' && document.hasFocus()) return;

    const currentUser = chatEntities.currentUser;
    if (!currentUser) return;
    const projectedPresence = chatEntities.presenceFor(currentUser);
    let storedPresence: string | null = null;
    try {
      storedPresence = window.localStorage.getItem('kaede.presence');
    } catch {
      // The live presence projection remains authoritative when storage is unavailable.
    }
    const currentPresence = resolveNotificationPresence(storedPresence, projectedPresence);
    const channel = chatEntities.channels.get(`${message.channel_id}@${message.channel_domain}`);
    if (!channel) return;
    const isDirectMessage = channel.guild_id === null;
    if (!isDirectMessage && !this.#guildPreferencesLoaded) return;
    const guildLevel =
      channel.guild_id && channel.guild_domain
        ? (this.#guildLevels.get(`${channel.guild_id}@${channel.guild_domain}`) ?? 'mentions')
        : 'mentions';
    if (!shouldNotifyForMessage(message, currentUser, isDirectMessage, guildLevel, currentPresence))
      return;

    const author =
      message.author ?? chatEntities.users.get(`${message.author_id}@${message.author_domain}`);
    const authorName = author?.display_name ?? author?.username ?? 'Someone';
    const guild =
      channel.guild_id && channel.guild_domain
        ? chatEntities.guilds.get(`${channel.guild_id}@${channel.guild_domain}`)
        : undefined;
    const title = isDirectMessage
      ? authorName
      : `${authorName} in #${channel.name ?? 'channel'}${guild ? ` · ${guild.name}` : ''}`;
    const body = message.content?.trim().slice(0, 180) || 'Sent an attachment';
    const icon = author?.avatar_hash
      ? new URL(assetUrl(author.avatar_hash, 'thumbnail_128', author), window.location.origin).href
      : undefined;
    if (isNativeDesktop()) {
      void nativeInvoke('native_notify', {
        title,
        body,
        sensitive: false,
        deepLink: guild ? guildChannelPath(guild, channel) : directMessagePath(channel)
      });
      return;
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
    } catch {
      // Notification permission can be revoked between the permission check and construction.
      this.refreshPermission();
    }
  }
}

export const browserNotifications = new BrowserNotifications();
