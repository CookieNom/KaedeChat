import { entityKey } from '$lib/chat/refs';
import type { Message, UserSummary } from '$lib/chat/types';
import { assetUrl } from '$lib/media/assets';
import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
import { chatEntities } from '$lib/stores/entities.svelte';

export function browserNotificationsFromSettings(settings: Record<string, unknown>): boolean {
  return settings.browser_notifications === true;
}

export function shouldNotifyForMessage(
  message: Message,
  currentUser: UserSummary,
  isDirectMessage: boolean
): boolean {
  if (message.author_id === currentUser.id && message.author_domain === currentUser.origin_domain) {
    return false;
  }
  return (
    isDirectMessage ||
    message.mention_user_refs.some(
      (reference) =>
        reference.id === currentUser.id && reference.origin_domain === currentUser.origin_domain
    )
  );
}

class BrowserNotifications {
  enabled = $state(false);
  permission = $state<NotificationPermission>('default');

  get supported(): boolean {
    return typeof window !== 'undefined' && 'Notification' in window;
  }

  apply(settings: Record<string, unknown>): void {
    this.enabled = browserNotificationsFromSettings(settings);
    this.refreshPermission();
  }

  refreshPermission(): void {
    this.permission = this.supported ? Notification.permission : 'denied';
  }

  async requestPermission(): Promise<NotificationPermission> {
    if (!this.supported) {
      this.permission = 'denied';
      return this.permission;
    }
    this.permission = await Notification.requestPermission();
    return this.permission;
  }

  disable(): void {
    this.enabled = false;
  }

  notifyMessage(message: Message): void {
    if (!this.enabled || !this.supported || Notification.permission !== 'granted') return;
    if (document.visibilityState === 'visible' && document.hasFocus()) return;

    const currentUser = chatEntities.currentUser;
    if (!currentUser) return;
    const channel = chatEntities.channels.get(`${message.channel_id}@${message.channel_domain}`);
    if (!channel) return;
    const isDirectMessage = channel.guild_id === null;
    if (!shouldNotifyForMessage(message, currentUser, isDirectMessage)) return;

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
