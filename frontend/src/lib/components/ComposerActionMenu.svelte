<script lang="ts">
  import Icon from './Icon.svelte';

  let {
    canAttach,
    canPoll,
    disabled = false,
    onAttach,
    onPoll
  }: {
    canAttach: boolean;
    canPoll: boolean;
    disabled?: boolean;
    onAttach: () => void;
    onPoll: () => void;
  } = $props();

  let open = $state(false);
  let root = $state<HTMLElement | null>(null);

  function choose(action: () => void) {
    open = false;
    action();
  }
</script>

<svelte:window
  onclick={(event) => {
    if (open && !root?.contains(event.target as Node)) open = false;
  }}
  onkeydown={(event) => {
    if (open && event.key === 'Escape') {
      event.preventDefault();
      open = false;
    }
  }}
/>

<div bind:this={root} class="composer-action-menu">
  <button
    class="attach-button"
    class:active={open}
    type="button"
    {disabled}
    aria-label="Open attachment and poll menu"
    title="Add"
    aria-haspopup="menu"
    aria-expanded={open}
    onclick={(event) => {
      event.stopPropagation();
      open = !open;
    }}
  >
    <Icon name="plus" size={21} />
  </button>
  {#if open}
    <div class="composer-action-popover" role="menu" aria-label="Add to message">
      <button type="button" role="menuitem" disabled={!canAttach} onclick={() => choose(onAttach)}>
        <Icon name="upload" size={18} />
        <span><strong>Upload a File</strong><small>Add photos, video, or documents</small></span>
      </button>
      <button type="button" role="menuitem" disabled={!canPoll} onclick={() => choose(onPoll)}>
        <Icon name="poll" size={18} />
        <span><strong>Create Poll</strong><small>Ask up to 10 answer choices</small></span>
      </button>
    </div>
  {/if}
</div>

<style>
  .composer-action-menu {
    position: relative;
    flex: 0 0 auto;
  }

  .attach-button {
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    padding: 0;
    border: 0;
    border-radius: 50%;
    color: var(--text-muted);
    background: transparent;
    cursor: pointer;
  }

  .attach-button:hover:not(:disabled),
  .attach-button.active {
    color: var(--text-primary);
    background: var(--surface-hover);
  }

  .attach-button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .composer-action-popover {
    position: absolute;
    z-index: 80;
    bottom: calc(100% + 10px);
    left: 0;
    display: grid;
    width: min(280px, calc(100vw - 32px));
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface-raised);
    box-shadow: 0 16px 38px rgb(0 0 0 / 0.34);
  }

  .composer-action-popover button {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 10px;
    align-items: center;
    min-height: 52px;
    padding: 8px 10px;
    border: 0;
    border-radius: 8px;
    color: var(--text-primary);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .composer-action-popover button:hover:not(:disabled),
  .composer-action-popover button:focus-visible {
    background: var(--surface-hover);
  }

  .composer-action-popover button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .composer-action-popover span {
    display: grid;
    gap: 2px;
  }

  .composer-action-popover small {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
</style>
