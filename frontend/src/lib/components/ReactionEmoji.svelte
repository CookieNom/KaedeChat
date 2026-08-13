<script lang="ts">
  import { customEmojiUrl } from '$lib/chat/emojis';

  let { value }: { value: string } = $props();
  const match = $derived(
    /^<a?:([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]+)>$/.exec(value)
  );
</script>

{#if match}
  <img
    class="reaction-emoji-image"
    src={customEmojiUrl(match[2], match[3])}
    alt={`:${match[1]}:`}
  />
{:else}
  <span aria-hidden="true">{value}</span>
{/if}

<style>
  .reaction-emoji-image {
    width: 1.35em;
    height: 1.35em;
    object-fit: contain;
  }
</style>
