<script lang="ts">
  import type { Snippet } from 'svelte';
  import { isAttachmentSpoiler } from '$lib/media/spoilers';
  let { filename, identity, children }: { filename: string; identity: string; children: Snippet } =
    $props();
  let revealed = $state('');
  const token = $derived(`${identity}:${filename}`);
</script>

{#if isAttachmentSpoiler(filename) && revealed !== token}
  <button
    class="spoiler-cover"
    type="button"
    onclick={() => (revealed = token)}
    aria-label="Reveal spoiler attachment"
  >
    <strong>SPOILER</strong><span>Click to reveal</span>
  </button>
{:else}
  {#if isAttachmentSpoiler(filename)}
    <button class="spoiler-hide" type="button" onclick={() => (revealed = '')}>Hide spoiler</button>
  {/if}
  {@render children()}
{/if}

<style>
  .spoiler-cover {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 240px;
    max-width: 100%;
    min-height: 120px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface-raised);
    color: var(--text);
    cursor: pointer;
  }
  .spoiler-cover span {
    font-size: 0.8rem;
  }
  .spoiler-hide {
    display: block;
    margin-bottom: 4px;
    font-size: 0.75rem;
  }
</style>
