<script lang="ts">
  import { customEmojiUrl, type CustomEmojiOption } from '$lib/chat/emojis';
  import type { ForumTag } from '$lib/chat/types';

  let {
    tag,
    guildId,
    guildDomain,
    customEmojis = []
  }: {
    tag: ForumTag;
    guildId: string | null;
    guildDomain: string | null;
    customEmojis?: CustomEmojiOption[];
  } = $props();

  const customEmoji = $derived(
    tag.emoji_id
      ? (customEmojis.find(
          (emoji) =>
            emoji.id === tag.emoji_id &&
            emoji.guild_id === guildId &&
            emoji.guild_domain === guildDomain
        ) ?? null)
      : null
  );
  const customUrl = $derived(
    tag.emoji_id && guildDomain
      ? (customEmoji?.url ?? customEmojiUrl(tag.emoji_id, guildDomain))
      : ''
  );
</script>

{#if customUrl}
  <img class="forum-tag-emoji" src={customUrl} alt={`:${customEmoji?.name ?? tag.name}:`} />
{:else if tag.emoji_name}
  <span class="forum-tag-emoji" aria-hidden="true">{tag.emoji_name}</span>
{/if}

<style>
  img.forum-tag-emoji {
    width: 1.15em;
    height: 1.15em;
    object-fit: contain;
  }

  .forum-tag-emoji {
    flex: 0 0 auto;
  }
</style>
