<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { forwardedMessagePath } from '$lib/chat/interactions';
  import { stickerUrl } from '$lib/chat/stickers';
  import type { Message, MessageSnapshot, Role, UserSummary } from '$lib/chat/types';
  import { isVoiceMessage } from '$lib/chat/voice-messages';
  import { downloadEncryptedFile } from '$lib/e2ee/media';
  import { attachmentMediaPath, authenticatedMedia } from '$lib/media/authenticated';
  import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import Markdown from './Markdown.svelte';
  import MessageComponents from './MessageComponents.svelte';
  import RichEmbed from './RichEmbed.svelte';
  import VoiceMessagePlayer from './VoiceMessagePlayer.svelte';
  import EncryptedVoiceMessagePlayer from './EncryptedVoiceMessagePlayer.svelte';

  let {
    message,
    mentionUsers = [],
    mentionRoles = [],
    allowExternalMedia = true,
    allowEncryptedManifests = false
  }: {
    message: Message;
    mentionUsers?: UserSummary[];
    mentionRoles?: Role[];
    allowExternalMedia?: boolean;
    /** Client-authenticated only; network snapshots must never inject file keys. */
    allowEncryptedManifests?: boolean;
  } = $props();

  interface ForwardAccess {
    source_channel_ref: string;
    source_message_ref: string;
  }

  let snapshot = $state<MessageSnapshot | null>(null);
  let loading = $state(false);
  let unavailable = $state('');
  let attachmentError = $state('');
  let sourceHref = $state('');
  const voiceAttachment = $derived(
    snapshot && isVoiceMessage(snapshot) ? (snapshot.attachments[0] ?? null) : null
  );

  function materialFromMessage(source: Message): MessageSnapshot {
    return {
      content: source.content,
      sticker_items: source.sticker_items ?? [],
      embeds: source.embeds ?? [],
      components: source.components ?? [],
      attachments: source.attachments ?? [],
      message_type: source.message_type,
      flags: source.flags,
      created_at: source.created_at,
      edited_at: source.edited_at
    };
  }

  function isForwardAccess(value: unknown): value is ForwardAccess {
    if (!value || typeof value !== 'object') return false;
    const record = value as Record<string, unknown>;
    return (
      typeof record.source_channel_ref === 'string' && typeof record.source_message_ref === 'string'
    );
  }

  function linkFor(access: ForwardAccess): string {
    const channel = chatEntities.channels.get(access.source_channel_ref);
    if (!channel) return '';
    const base =
      channel.guild_id && channel.guild_domain
        ? guildChannelPath({ id: channel.guild_id, origin_domain: channel.guild_domain }, channel)
        : directMessagePath(channel);
    return `${base}?around=${encodeURIComponent(access.source_message_ref)}`;
  }

  async function downloadEncryptedSnapshotAttachment(
    attachment: MessageSnapshot['attachments'][number]
  ) {
    if (!allowEncryptedManifests || !attachment.encrypted_manifest) return;
    attachmentError = '';
    try {
      await downloadEncryptedFile(
        attachment.encrypted_manifest,
        attachment.history_media_url,
        attachment.private_media_url
      );
    } catch (caught) {
      attachmentError = userErrorMessage(
        caught,
        'Could not decrypt this forwarded file on this device.'
      );
    }
  }

  $effect(() => {
    const embedded = message.message_snapshots?.[0]?.message ?? null;
    const legacyEmbedded = message.forwarded_message ?? null;
    snapshot = embedded ?? (legacyEmbedded ? materialFromMessage(legacyEmbedded) : null);
    sourceHref = '';
    unavailable = '';

    if (!embedded && !legacyEmbedded && !message.forwarded_message_ref) {
      loading = false;
      unavailable = 'The forwarded snapshot is unavailable.';
      return;
    }

    const destinationChannel = `${message.channel_id}@${message.channel_domain}`;
    const destinationMessage = `${message.id}@${message.origin_domain}`;
    const controller = new AbortController();
    loading = snapshot === null;
    void api<ForwardAccess | (Message & { source_channel_ref?: string })>(
      forwardedMessagePath(destinationChannel, destinationMessage),
      { signal: controller.signal }
    )
      .then((result) => {
        if (controller.signal.aborted) return;
        if (isForwardAccess(result)) {
          sourceHref = linkFor(result);
        } else if (snapshot === null) {
          snapshot = materialFromMessage(result);
          if (result.source_channel_ref) {
            sourceHref = linkFor({
              source_channel_ref: result.source_channel_ref,
              source_message_ref: `${result.id}@${result.origin_domain}`
            });
          }
        }
      })
      .catch(() => {
        if (!controller.signal.aborted && snapshot === null) {
          unavailable = 'The forwarded snapshot is unavailable.';
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });
    return () => controller.abort();
  });
</script>

<aside class="forwarded-message" aria-label="Forwarded message snapshot">
  <header>
    <span aria-hidden="true">↪</span><strong>Forwarded</strong>
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- linkFor composes only route helpers that already call resolve() -->
    {#if sourceHref}<a href={sourceHref}>Go to source</a>{/if}
  </header>
  {#if loading && !snapshot}
    <p class="muted" role="status">Loading the forwarded snapshot…</p>
  {:else if unavailable && !snapshot}
    <p class="muted">{unavailable}</p>
  {:else if snapshot}
    <time datetime={snapshot.created_at}
      >Snapshot · {new Date(snapshot.created_at).toLocaleString()}</time
    >
    {#if snapshot.content}<Markdown content={snapshot.content} {mentionUsers} {mentionRoles} />{/if}
    {#if snapshot.sticker_items?.length}
      <div class="forwarded-stickers" aria-label="Forwarded stickers">
        {#each snapshot.sticker_items as sticker (`${sticker.id}@${sticker.origin_domain}`)}
          <img
            src={stickerUrl(sticker.id, sticker.origin_domain)}
            alt={sticker.name}
            loading="lazy"
          />
        {/each}
      </div>
    {/if}
    {#each snapshot.embeds as embed, index (`${index}:${embed.title ?? ''}`)}
      <RichEmbed
        {embed}
        attachments={snapshot.attachments}
        {mentionUsers}
        {mentionRoles}
        {allowExternalMedia}
      />
    {/each}
    {#if snapshot.components.length}
      <MessageComponents
        components={snapshot.components}
        attachments={snapshot.attachments}
        users={mentionUsers}
        roles={mentionRoles}
        disabled
        {allowExternalMedia}
      />
    {/if}
    {#if snapshot.attachments.length}
      <div class="forwarded-attachments">
        {#each snapshot.attachments as attachment (`${attachment.id}@${attachment.origin_domain}`)}
          {#if allowEncryptedManifests && attachment.encrypted_manifest?.duration_millis !== undefined && attachment.encrypted_manifest.waveform !== undefined}
            <EncryptedVoiceMessagePlayer
              manifest={attachment.encrypted_manifest}
              {attachment}
              onError={(caught) =>
                (attachmentError = userErrorMessage(
                  caught,
                  'Could not decrypt this forwarded voice message on this device.'
                ))}
            />
          {:else if allowEncryptedManifests && attachment.encrypted_manifest}
            <button
              type="button"
              class="forwarded-file"
              onclick={() => void downloadEncryptedSnapshotAttachment(attachment)}
            >
              🔒 {attachment.filename}
            </button>
          {:else if voiceAttachment && attachment === voiceAttachment}
            <VoiceMessagePlayer {attachment} />
          {:else if attachment.content_type.startsWith('image/')}
            <img
              use:authenticatedMedia={{
                path: attachmentMediaPath(
                  attachment.origin_domain,
                  attachment.id,
                  'thumbnail_512',
                  attachment.history_media_url
                ),
                contentType: attachment.content_type
              }}
              alt={attachment.filename}
              loading="lazy"
            />
          {:else}
            <span class="forwarded-file">{attachment.filename}</span>
          {/if}
        {/each}
      </div>
      {#if attachmentError}<p class="attachment-error" role="alert">{attachmentError}</p>{/if}
    {/if}
  {:else}
    <p class="muted">The forwarded snapshot is unavailable.</p>
  {/if}
</aside>

<style>
  .forwarded-message {
    width: min(540px, 100%);
    margin-top: 8px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    background: var(--surface-subtle);
  }
  header {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 7px;
    color: var(--text-muted);
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  header a {
    margin-left: auto;
    color: var(--accent);
    text-transform: none;
    letter-spacing: 0;
  }
  time,
  .muted {
    color: var(--text-muted);
    font-size: 0.74rem;
  }
  p {
    margin: 4px 0;
  }
  .forwarded-attachments {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    margin-top: 8px;
  }
  .forwarded-stickers {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .forwarded-stickers img {
    width: min(150px, 38vw);
    height: 150px;
    object-fit: contain;
  }
  .forwarded-attachments img {
    max-width: min(320px, 100%);
    max-height: 240px;
    border-radius: 6px;
    object-fit: contain;
  }
  .forwarded-file {
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px 10px;
    color: var(--text-muted);
    font-size: 0.8rem;
  }
  button.forwarded-file {
    background: transparent;
    cursor: pointer;
  }
  .attachment-error {
    color: var(--danger);
    font-size: 0.78rem;
  }
</style>
