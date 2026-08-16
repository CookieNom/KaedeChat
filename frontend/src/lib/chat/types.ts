import type { FederatedIdentity } from './refs';

export interface UserSummary {
  id: string;
  origin_domain: string;
  username: string;
  display_name: string | null;
  avatar_hash: string | null;
  banner_hash?: string | null;
  bio?: string | null;
  custom_status?: string | null;
  profile_version?: string;
  profile_resolved?: boolean;
  account_type?: 'human' | 'bot';
  bot?: boolean;
  handle: string;
}

export type PresenceStatus = 'online' | 'idle' | 'dnd' | 'offline';

export interface Channel {
  id: string;
  origin_domain: string;
  guild_id: string | null;
  guild_domain: string | null;
  type: number;
  name: string | null;
  topic: string | null;
  position: number;
  parent_id: string | null;
  parent_domain: string | null;
  permissions_synced?: boolean;
  permissions?: string;
  rate_limit_per_user: number;
  federated_history_policy?: 'inherit' | 'disabled' | 'full_retained';
  encryption_mode?: 'plaintext' | 'e2ee';
  search_available?: boolean;
  last_message_id: string | null;
  last_message_domain: string | null;
  recipients?: UserSummary[];
  conversation_type?: 'direct' | 'group';
  owner_id?: string | null;
  owner_domain?: string | null;
  version?: string | null;
  /** Whether this instance retained only the newest part of a remote DM. */
  history_truncated?: boolean;
  history_retention?: 'authoritative' | 'rolling_replica_cache';
  history_remote_available?: boolean;
  oldest_available_message_ref?: FederatedIdentity | null;
  history_degraded_code?: 'FEDERATED_DM_HISTORY_TRUNCATED' | string | null;
}

export interface Guild {
  id: string;
  origin_domain: string;
  name: string;
  description: string | null;
  icon_hash: string | null;
  owner_id: string;
  owner_domain?: string;
  permissions?: string;
  actor_highest_role_id?: string;
  permission_generation: string;
  federated_history_policy?: 'disabled' | 'full_retained';
  history_policy_generation?: string;
  unavailable: boolean;
  sync_status?: 'ready' | 'syncing' | 'stale' | 'failed' | 'quota_paused';
  sync_error_code?: string | null;
  history_sync_status?: 'syncing' | 'retrying' | 'ready' | 'failed';
  history_sync_error_code?: string | null;
  history_sync_retry_after_ms?: number | null;
  history_sync_resource?: string | null;
  channels?: Channel[];
  roles?: Role[];
  emojis?: CustomEmoji[];
  emoji_limit?: number;
  emoji_max_bytes?: number;
  version?: string | null;
}

export interface CustomEmoji {
  id: string;
  origin_domain: string;
  guild_id: string;
  guild_domain: string;
  guild_name?: string;
  name: string;
  animated: boolean;
  media_hash: string | null;
  version?: string | null;
}

export interface Role {
  id: string;
  origin_domain: string;
  guild_id: string;
  guild_domain: string;
  name: string;
  color: number;
  permissions: string;
  position: number;
  hoist: boolean;
  mentionable: boolean;
  version?: string | null;
}

export interface Message {
  id: string;
  origin_domain: string;
  channel_id: string;
  channel_domain: string;
  author_id: string;
  author_domain: string;
  author: UserSummary | null;
  content: string | null;
  e2ee?: Record<string, unknown> | null;
  message_type: number;
  flags: number;
  client_nonce: string | null;
  referenced_message_id: string | null;
  referenced_message_domain: string | null;
  mention_user_refs: FederatedIdentity[];
  attachments?: Attachment[];
  reaction_counts?: Record<string, number>;
  reacted_emoji?: string[];
  webhook_id?: string | null;
  webhook?: { id: string | null; name: string; avatar_hash: string | null } | null;
  edited_at: string | null;
  deleted_at: string | null;
  created_at: string;
  delivery_status?: 'pending' | 'retrying' | 'delivered' | 'failed';
  pending?: boolean;
  queued?: boolean;
  failed?: boolean;
  failure_reason?: string;
  retryable?: boolean;
  /** True only on the oldest item of a final on-demand authority page. */
  history_page_complete?: boolean;
  /** Nonterminal failure while extending this page from the DM authority. */
  history_page_error_code?: string;
  history_page_retry_after_ms?: number;
}

export interface ReactionUsersResponse {
  items: UserSummary[];
  total: number;
  next_after: string | null;
}

export interface MessageSearchResult {
  message: Message;
  channel: Channel;
  guild: Guild | null;
  snippet: string;
}

export interface MessageSearchResponse {
  results: MessageSearchResult[];
  next_cursor: string | null;
  coverage: {
    local: 'complete' | 'cached' | 'unavailable';
    authority: 'not_needed' | 'not_queried' | 'complete' | 'unsupported' | 'unavailable';
  };
  encrypted_channel_refs: string[];
  indexing: boolean;
}

export interface Attachment {
  id: string;
  origin_domain: string;
  filename: string;
  content_type: string;
  size: number;
  width: number | null;
  height: number | null;
  blurhash: string | null;
  scan_status: 'pending' | 'clean' | 'infected' | 'failed';
  variants: Record<string, { width?: number; height?: number; content_type?: string }>;
  /**
   * Same-origin, authenticated stream for media returned by an on-demand
   * federated history page. These attachments are intentionally not inserted
   * into the local replica cache, so their ordinary attachment route may not
   * exist.
   */
  history_media_url?: string | null;
}

export interface ReadStateStatus {
  channel_id: string;
  channel_domain: string;
  guild_id: string | null;
  guild_domain: string | null;
  last_message_id: string | null;
  last_message_domain: string | null;
  read_message_id: string | null;
  read_message_domain: string | null;
  mention_count: number;
  unread: boolean;
}

export interface GuildMemberSummary {
  guild_id: string;
  guild_domain: string;
  user: UserSummary;
  nickname: string | null;
  role_ids: string[];
  timeout_until?: string | null;
  timeout_indefinite?: boolean;
  timeout_reason?: string | null;
  presence?: PresenceStatus;
}

export interface Relationship {
  type: 'friend' | 'pending_in' | 'pending_out' | 'blocked';
  user: UserSummary;
  created_at: string;
  updated_at: string;
}
