<script lang="ts">
  import { customEmojiUrl, type CustomEmojiOption } from '$lib/chat/emojis';
  import { t } from '$lib/ui/locale';
  import EmojiPicker from './EmojiPicker.svelte';
  import Icon from './Icon.svelte';

  let {
    emojiId,
    emojiName,
    guildDomain,
    customEmojis,
    label,
    disabled = false,
    onChange
  }: {
    emojiId?: string | null;
    emojiName?: string | null;
    guildDomain: string;
    customEmojis: CustomEmojiOption[];
    label: string;
    disabled?: boolean;
    onChange: (emoji: { emoji_id: string | null; emoji_name: string | null }) => void;
  } = $props();
  const id = $props.id();
  let panel: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let open = $state(false);
  const custom = $derived(customEmojis.find((emoji) => emoji.id === emojiId));

  function close() {
    panel.hidePopover();
    open = false;
    trigger.focus();
  }

  function choose(value: string) {
    const selected = customEmojis.find((emoji) => emoji.value === value);
    onChange({ emoji_id: selected?.id ?? null, emoji_name: selected ? null : value });
    close();
  }
</script>

<div class="forum-emoji-field">
  <button
    bind:this={trigger}
    type="button"
    class="emoji-trigger"
    aria-label={label}
    aria-haspopup="dialog"
    aria-expanded={open}
    popovertarget={id}
    title={$t('ui_choose_an_emoji_54bc3777')}
    {disabled}
  >
    {#if emojiId}
      <img src={custom?.url ?? customEmojiUrl(emojiId, guildDomain)} alt={custom?.name ?? label} />
    {:else}
      {emojiName || $t('ui_emoji_61ad8976')}
    {/if}
    <Icon name="chevron-down" size={14} />
  </button>
  {#if emojiId || emojiName}
    <button
      class="clear-emoji"
      type="button"
      aria-label={`${$t('ui_remove_c3812fc4')}: ${label}`}
      {disabled}
      onclick={() => onChange({ emoji_id: null, emoji_name: null })}>×</button
    >
  {/if}
</div>
<div
  bind:this={panel}
  {id}
  popover="auto"
  class="emoji-popover"
  role="presentation"
  ontoggle={(event) => (open = event.newState === 'open')}
  onkeydown={(event) => {
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) {
      event.preventDefault();
    }
    if (event.key === 'Escape') {
      event.stopPropagation();
      event.preventDefault();
      close();
    }
  }}
>
  {#if open}
    <EmojiPicker inline {customEmojis} onSelect={choose} onClose={close} />
  {/if}
</div>

<style>
  .forum-emoji-field {
    display: flex;
    align-items: center;
    min-width: 0;
    width: fit-content;
    gap: 4px;
  }
  .emoji-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-width: 0;
    min-height: 36px;
    flex: 1;
    padding: 4px 8px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    background: var(--accent-soft);
    color: var(--accent-text);
    font-weight: 700;
    cursor: pointer;
  }
  .emoji-trigger:hover:not(:disabled),
  .emoji-trigger[aria-expanded='true'] {
    background: var(--accent);
    color: var(--on-accent);
  }
  .emoji-trigger:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .emoji-trigger img {
    width: 24px;
    height: 24px;
    object-fit: contain;
  }
  .clear-emoji {
    padding: 4px;
    border: 0;
    background: transparent;
    color: var(--text-muted);
  }
  .emoji-popover {
    padding: 0;
    border: 0;
    border-radius: 18px;
    background: transparent;
    color: var(--text);
    overflow: visible;
  }
</style>
