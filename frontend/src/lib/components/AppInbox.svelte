<script lang="ts">
  import { t } from '$lib/ui/locale';
  import Icon from './Icon.svelte';
  import ReadInbox from './ReadInbox.svelte';
  let open = $state(false);
  let button: HTMLButtonElement;
  function close() {
    open = false;
    button?.focus({ preventScroll: true });
  }
</script>

<header class="app-utility-bar">
  <span>Kaede</span>
  <button
    bind:this={button}
    class:active={open}
    title={$t('chat_inbox')}
    aria-label={$t('chat_inbox')}
    aria-haspopup="dialog"
    aria-expanded={open}
    onclick={() => (open = !open)}><Icon name="inbox" /></button
  >
</header>
{#if open}<ReadInbox onClose={close} />{/if}

<style>
  .app-utility-bar {
    height: var(--app-utility-height);
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 12px 0 20px;
    background: var(--rail);
    color: var(--text-muted);
    border-bottom: 1px solid var(--line-soft);
  }
  span {
    font: 600 12px var(--font-display);
    letter-spacing: 0.02em;
  }
  button {
    display: grid;
    place-items: center;
    width: 32px;
    height: 28px;
    padding: 0;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--text-soft);
    cursor: pointer;
  }
  button:hover,
  button.active {
    background: var(--rail-hover);
    color: var(--text);
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  @media (max-width: 700px) {
    button {
      width: 44px;
      height: 40px;
    }
  }
</style>
