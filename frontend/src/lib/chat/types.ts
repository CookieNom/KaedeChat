import type { FederatedIdentity } from './refs';
import type { MessageEmbed, MessageLayoutComponent, MessagePoll } from './rich-content';

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
  age_assurance_state?: 'unknown' | 'adult' | 'minor';
  bot?: boolean;
  handle: string;
}

export type PresenceStatus = 'online' | 'idle' | 'dnd' | 'offline';

export interface ForumTag {
  id: string;
  name: string;
  moderated?: boolean;
  emoji_id?: string | null;
  emoji_name?: string | null;
}

export interface ThreadMember {
  id?: string;
  thread_domain?: string;
  user_id?: string;
  user_domain?: string;
  join_timestamp?: string;
  flags?: number;
  notification_level?: 'inherit' | 'all' | 'mentions' | 'none';
  user?: UserSummary;
}

export interface Channel {
  id: string;
  origin_domain: string;
  guild_id: string | null;
  guild_domain: string | null;
  type: number;
  name: string | null;
  topic: string | null;
  nsfw?: boolean;
  position: number;
  parent_id: string | null;
  parent_domain: string | null;
  permissions_synced?: boolean;
  permissions?: string;
  rate_limit_per_user: number;
  /** Voice-only configuration. A null rtc_region means automatic selection. */
  bitrate?: number | null;
  user_limit?: number | null;
  rtc_region?: string | null;
  video_quality_mode?: 1 | 2 | null;
  federated_history_policy?: 'inherit' | 'disabled' | 'full_retained';
  encryption_mode?: 'plaintext' | 'e2ee';
  encryption_state?:
    'plaintext' | 'legacy' | 'proposed' | 'activating' | 'active' | 'rekeying' | 'failed';
  encryption_policy_generation?: string;
  encryption_protocol?: string | null;
  encryption_suite?: string | null;
  encryption_group_id?: string | null;
  encryption_epoch?: string | null;
  encryption_activated_at?: string | null;
  search_available?: boolean;
  last_message_id: string | null;
  last_message_domain: string | null;
  recipients?: UserSummary[];
  conversation_type?: 'direct' | 'group';
  owner_id?: string | null;
  owner_domain?: string | null;
  archived?: boolean;
  locked?: boolean;
  invitable?: boolean;
  auto_archive_duration?: number;
  archive_timestamp?: string | null;
  message_count?: number;
  member_count?: number;
  total_message_sent?: number;
  created_at?: string;
  flags?: number | string;
  applied_tag_ids?: string[];
  available_tags?: ForumTag[];
  default_auto_archive_duration?: number;
  default_thread_rate_limit_per_user?: number;
  default_sort_order?: 0 | 1 | 'recent_activity' | 'creation_date' | null;
  default_forum_layout?: 0 | 1 | 2 | 'list' | 'gallery' | null;
  default_reaction_emoji?: { emoji_id?: string | null; emoji_name?: string | null } | null;
  e2ee_required?: boolean;
  default_thread_encryption_mode?: 'plaintext' | 'e2ee';
  member?: ThreadMember | null;
  starter_message?: Message | null;
  last_message?: Message | null;
  pinned?: boolean;
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
  stickers?: GuildSticker[];
  sticker_limit?: number;
  sticker_max_bytes?: number;
  sticker_background_removal_enabled?: boolean;
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
  available?: boolean;
  roles?: string[];
  creator_id?: string;
  creator_domain?: string;
  version?: string | null;
}

export interface GuildSticker {
  id: string;
  origin_domain: string;
  guild_id: string;
  guild_domain: string;
  guild_name?: string;
  name: string;
  description: string | null;
  animated: boolean;
  media_hash: string | null;
  available?: boolean;
  tags?: string[];
  creator_id?: string;
  creator_domain?: string;
  version?: string | null;
}

/** Immutable sticker snapshot returned on a message. */
export interface StickerItem {
  id: string;
  origin_domain: string;
  name: string;
  format_type: 1 | 2 | 3 | 4;
  media_hash: string;
}

export interface Role {
  id: string;
  origin_domain: string;
  guild_id: string;
  guild_domain: string;
  name: string;
  icon_hash?: string | null;
  color: number;
  permissions: string;
  position: number;
  hoist: boolean;
  mentionable: boolean;
  version?: string | null;
}

export type InteractionMetadataIntegrationType = 'guild_install' | 'user_install' | 'dm_capability';

export interface InteractionMetadataUser {
  id: string;
  origin_domain: string;
  username: string;
  display_name: string | null;
  avatar_hash: string | null;
  bot: boolean;
}

/** Durable, authority-sanitized attribution for an application response. */
export interface MessageInteractionMetadata {
  id: string;
  origin_domain: string;
  interaction_ref: string;
  type: 'command' | 'component' | 'modal_submit';
  user: InteractionMetadataUser;
  user_ref: string;
  application_ref: string;
  integration_type: InteractionMetadataIntegrationType;
  authorizing_integration_owners: Partial<Record<InteractionMetadataIntegrationType, string>>;
  command_name?: string;
  command_type?: 'chat_input' | 'user' | 'message';
  target_user?: InteractionMetadataUser | null;
  target_user_ref?: string | null;
  target_message_id?: string | null;
  target_message_domain?: string | null;
  target_message_ref?: string | null;
  original_response_message_id?: string | null;
  original_response_message_domain?: string | null;
  original_response_message_ref?: string | null;
  interacted_message_id?: string | null;
  interacted_message_domain?: string | null;
  interacted_message_ref?: string | null;
  triggering_interaction_metadata?: MessageInteractionMetadata | null;
}

/** Discord message-reference fields plus the domains required for federation. */
export interface MessageReference {
  type?: number;
  message_id?: string | null;
  message_domain?: string | null;
  channel_id?: string | null;
  channel_domain?: string | null;
  guild_id?: string | null;
  guild_domain?: string | null;
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
  sticker_items?: StickerItem[];
  /** Stable server flag for Discord-compatible text-to-speech playback. */
  tts?: boolean;
  embeds?: MessageEmbed[];
  components?: MessageLayoutComponent[];
  application_id?: string | null;
  application_domain?: string | null;
  view_version?: number;
  view_persistent?: boolean;
  view_expires_at?: string | null;
  interaction_integration_type?: 'guild_install' | 'user_install' | 'dm_capability' | null;
  interaction_installation_ref?: string | null;
  interaction_installation_revision?: string | null;
  interaction_metadata?: MessageInteractionMetadata | null;
  forwarded_message_id?: string | null;
  forwarded_message_domain?: string | null;
  forwarded_message_ref?: string | null;
  /** Legacy live projection; new forwards use author-free immutable snapshots below. */
  forwarded_message?: Message | null;
  /** Discord-compatible, immutable and author-free forwarded message material. */
  message_snapshots?: Array<{ message: MessageSnapshot }>;
  poll?: MessagePoll | null;
  /** Authority-authenticated, label-free Discord type-46 result projection. */
  poll_result?: Record<string, unknown> | null;
  e2ee?: Record<string, unknown> | null;
  encryption_policy_generation?: string;
  encryption_epoch?: string | null;
  /** Client-only plaintext produced after authenticated E2EE decryption. Never persisted or relayed. */
  decrypted_content?: string | null;
  /** Client-only proof that the encrypted body authenticated even when it has no text. */
  e2ee_verified?: boolean;
  /** Client-only attachment keys authenticated inside the decrypted MLS application. */
  decrypted_attachments?: import('$lib/e2ee/media').EncryptedFileManifest[];
  /** Client-only notification policy authenticated inside rich-v2 ciphertext. */
  decrypted_allowed_mentions?: import('$lib/e2ee/client').EncryptedAllowedMentions;
  /** Client-only author-free snapshot authenticated inside rich-v2 ciphertext. */
  decrypted_forward_snapshot?: Record<string, unknown> | null;
  message_type: number;
  flags: number;
  client_nonce: string | null;
  referenced_message_id: string | null;
  referenced_message_domain: string | null;
  message_reference?: MessageReference | null;
  /** Resolved source/reply payload. Type-21 thread starters keep their body only here. */
  referenced_message?: Message | null;
  mention_user_refs: FederatedIdentity[];
  /** Canonical public mention intent retained for Discord-style search. */
  mention_role_refs?: FederatedIdentity[];
  mention_everyone?: boolean;
  attachments?: Attachment[];
  reaction_counts?: Record<string, number>;
  reacted_emoji?: string[];
  /** Present on the modern channel-pins projection or client reconciliation. */
  pinned?: boolean;
  pinned_at?: string;
  webhook_id?: string | null;
  webhook?: {
    id: string | null;
    origin_domain?: string | null;
    ref?: string | null;
    name: string;
    avatar_hash: string | null;
    avatar_url?: string | null;
  } | null;
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
  /** The referenced thread starter was deleted or is outside retained/readable history. */
  content_unavailable?: boolean;
  /** Active thread projected onto its parent-channel starter or thread-created notice. */
  thread?: Channel | null;
}

export interface MessageSnapshot {
  content: string | null;
  sticker_items?: StickerItem[];
  embeds: MessageEmbed[];
  components: MessageLayoutComponent[];
  attachments: Attachment[];
  mention_user_refs?: FederatedIdentity[];
  message_snapshots?: Array<{ message: MessageSnapshot }>;
  message_type: number;
  flags: number;
  created_at: string;
  edited_at?: string | null;
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
  scan_status: 'pending' | 'clean' | 'rejected' | 'infected' | 'failed' | 'encrypted';
  encryption_mode?: 'plaintext' | 'e2ee';
  encryption_protocol?: 'kaede-file-v1' | null;
  variants: Record<string, { width?: number; height?: number; content_type?: string }>;
  /** Discord voice-message metadata. Present together on the single audio attachment. */
  duration_secs?: number | null;
  waveform?: string | null;
  /**
   * Same-origin, authenticated stream for media returned by an on-demand
   * federated history page. These attachments are intentionally not inserted
   * into the local replica cache, so their ordinary attachment route may not
   * exist.
   */
  history_media_url?: string | null;
  /** Same-origin authenticated base path for an isolated interaction file. */
  private_media_url?: string | null;
  /** Client-only manifest authenticated inside an encrypted forwarded snapshot. */
  encrypted_manifest?: import('$lib/e2ee/media').EncryptedFileManifest;
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
  temporary?: boolean;
  presence?: PresenceStatus;
}

export interface Relationship {
  type: 'friend' | 'pending_in' | 'pending_out' | 'blocked';
  user: UserSummary;
  created_at: string;
  updated_at: string;
}
