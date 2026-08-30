<script lang="ts">
  import { customEmojiUrl } from '$lib/chat/emojis';
  import { reactionEmojiPresentation } from '$lib/chat/reactions';

  let { value }: { value: string } = $props();
  const match = $derived(
    /^<a?:([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]+)>$/.exec(value)
  );
  let presentation = $state('');

  $effect(() => {
    const source = value;
    presentation = '';
    if (match) return;
    void reactionEmojiPresentation(source).then((resolved) => {
      if (value === source) presentation = resolved;
    });
  });
</script>

{#if match}
  <img
    class="reaction-emoji-image"
    src={customEmojiUrl(match[2], match[3])}
    alt={`:${match[1]}:`}
  />
{:else}
  <span class="unicode-reaction-emoji" aria-hidden="true">{presentation || value}</span>
{/if}

<style>
  .reaction-emoji-image {
    width: 1.35em;
    height: 1.35em;
    object-fit: contain;
  }

  .unicode-reaction-emoji {
    font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif;
  }
</style>
