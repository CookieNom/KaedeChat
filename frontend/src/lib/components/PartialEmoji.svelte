<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { customEmojiUrl } from '$lib/chat/emojis';
  import type { PartialEmoji } from '$lib/chat/rich-content';

  let {
    emoji,
    size = 18,
    decorative = false
  }: {
    emoji: PartialEmoji;
    size?: number;
    decorative?: boolean;
  } = $props();

  let custom = $derived.by(() => {
    if (!emoji.id) return null;
    const separator = emoji.id.lastIndexOf('@');
    if (separator <= 0 || separator === emoji.id.length - 1) return null;
    const id = emoji.id.slice(0, separator);
    const domain = emoji.id.slice(separator + 1);
    const url = customEmojiUrl(id, domain);
    return url
      ? { url, label: emoji.name ? `:${emoji.name}:` : $t('ui_custom_emoji_1596e05e') }
      : null;
  });
</script>

{#if custom}
  <img
    class="partial-emoji"
    src={custom.url}
    alt={decorative ? '' : custom.label}
    width={size}
    height={size}
    loading="lazy"
    decoding="async"
  />
{:else}
  <span class="unicode-emoji" aria-hidden={decorative ? 'true' : undefined}>{emoji.name ?? ''}</span
  >
{/if}

<style>
  .partial-emoji {
    display: inline-block;
    flex: 0 0 auto;
    object-fit: contain;
    vertical-align: -0.2em;
  }
  .unicode-emoji {
    line-height: 1;
  }
</style>
