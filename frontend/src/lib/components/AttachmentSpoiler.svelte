<script lang="ts">
  import { t } from '$lib/ui/locale';

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
    aria-label={$t('ui_reveal_spoiler_attachment_5d1fbb67')}
  >
    <strong>{$t('ui_spoiler_de665a83')}</strong><span>{$t('ui_click_to_reveal_dca25e98')}</span>
  </button>
{:else}
  {#if isAttachmentSpoiler(filename)}
    <button class="spoiler-hide" type="button" onclick={() => (revealed = '')}
      >{$t('ui_hide_spoiler_52190771')}</button
    >
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
