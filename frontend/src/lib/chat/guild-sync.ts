import type { Guild } from './types';

export interface GuildSyncGuidance {
  title: string;
  message: string;
  severity: 'status' | 'alert';
}

type GuildReplicaSyncState = Pick<Guild, 'sync_status' | 'sync_error_code'>;
type GuildHistorySyncState = Pick<
  Guild,
  | 'history_sync_status'
  | 'history_sync_error_code'
  | 'history_sync_retry_after_ms'
  | 'history_sync_resource'
>;

/** User-facing replica health shared by browser and supported Tauri desktop. */
export function guildReplicaSyncGuidance(guild: GuildReplicaSyncState): GuildSyncGuidance | null {
  if (guild.sync_status !== 'quota_paused') return null;
  const code = guild.sync_error_code;
  if (
    code === 'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED' ||
    code === 'KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED'
  ) {
    return {
      title: 'Guild updates are paused on this instance.',
      message:
        'This instance cannot cache another remote account needed by the guild. Contact your instance administrator; you do not need to delete your own messages.',
      severity: 'alert'
    };
  }
  if (
    code === 'FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED' ||
    code === 'KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED'
  ) {
    return {
      title: 'Guild updates are paused on this instance.',
      message:
        'This instance cannot cache another remote server needed by the guild. Contact your instance administrator; you do not need to delete your own messages.',
      severity: 'alert'
    };
  }
  return {
    title: 'Guild updates are paused on this instance.',
    message:
      'Its remote-guild cache is full, so recent messages or changes may be missing. Contact your instance administrator; you do not need to delete your own messages.',
    severity: 'alert'
  };
}

/** Nonblocking retained-history status shared by browser and Tauri desktop. */
export function guildHistorySyncGuidance(guild: GuildHistorySyncState): GuildSyncGuidance | null {
  if (guild.history_sync_status === 'retrying') {
    const seconds = Math.max(1, Math.ceil((guild.history_sync_retry_after_ms ?? 2_000) / 1_000));
    const title =
      guild.history_sync_error_code === 'KAED_FED_HISTORY_CAPACITY'
        ? 'Older guild history is waiting for capacity.'
        : 'Older guild history is temporarily delayed.';
    return {
      title,
      message: `Recent messages remain available. Kaede will retry automatically in about ${seconds} second${seconds === 1 ? '' : 's'}; no action is needed.`,
      severity: 'status'
    };
  }
  if (guild.history_sync_status !== 'failed') return null;
  if (guild.history_sync_error_code === 'FEDERATED_GUILD_HISTORY_LIMIT_REACHED') {
    const resource = guild.history_sync_resource?.replaceAll('_', ' ');
    return {
      title: 'Older guild history stopped at this instance’s safety limit.',
      message: `Recent and new messages still work. Ask your instance administrator to raise the federation history${resource ? ` ${resource}` : ''} limit if more retained history is needed.`,
      severity: 'alert'
    };
  }
  if (guild.history_sync_error_code === 'FEDERATED_GUILD_HISTORY_REJECTED') {
    return {
      title: 'Older guild history could not be safely imported.',
      message:
        'The remote instance returned history Kaede could not accept. Recent and new messages still work; contact your instance administrator if older history is required.',
      severity: 'alert'
    };
  }
  return {
    title: 'Older guild history could not be imported.',
    message:
      'Recent and new messages still work. Contact your instance administrator if older history remains unavailable.',
    severity: 'alert'
  };
}
