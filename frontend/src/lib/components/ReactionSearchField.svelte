<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { customEmojiToken, type CustomEmojiOption } from '$lib/chat/emojis';
  import type { CustomEmoji } from '$lib/chat/types';
  import { canonicalReactionEmoji } from '$lib/chat/reactions';
  import { assetUrl } from '$lib/media/assets';
  import { t } from '$lib/ui/locale';
  import EmojiPicker from './EmojiPicker.svelte';
  import ReactionEmoji from './ReactionEmoji.svelte';

  let {
    value = $bindable(''),
    customEmojis
  }: {
    value: string;
    customEmojis?: CustomEmojiOption[];
  } = $props();
  const id = $props.id();
  let panel: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let open = $state(false);
  let loadedEmojis = $state<CustomEmojiOption[]>([]);
  let loaded = false;
  let loading = $state(false);
  let error = $state('');

  async function loadCustomEmojis() {
    if (customEmojis !== undefined || loaded || loading) return;
    loading = true;
    error = '';
    try {
      loadedEmojis = (await api<CustomEmoji[]>('/users/@me/emojis'))
        .filter((emoji) => emoji.media_hash)
        .map((emoji) => ({
          ...emoji,
          url: assetUrl(emoji.media_hash!, 'thumbnail_128', emoji.origin_domain),
          value: customEmojiToken(emoji)
        }))
        .filter((emoji) => emoji.url && emoji.value);
      loaded = true;
    } catch (caught) {
      error = userErrorMessage(caught, $t('search_custom_emoji_error'));
    } finally {
      loading = false;
    }
  }

  function close() {
    panel.hidePopover();
    open = false;
    trigger.focus();
  }
</script>

<div class="reaction-filter">
  <span>{$t('search_reaction')}</span>
  <div class="controls">
    <button
      bind:this={trigger}
      type="button"
      class="choose-reaction"
      aria-label={value ? $t('search_change_reaction') : $t('search_choose_reaction')}
      aria-haspopup="dialog"
      aria-expanded={open}
      popovertarget={id}
    >
      {#if value}<ReactionEmoji {value} /><span>{$t('search_change_emoji')}</span>
      {:else}<span>{$t('ui_choose_an_emoji_54bc3777')}</span>{/if}
    </button>
    {#if value}
      <button
        type="button"
        class="clear-reaction"
        onclick={() => (value = '')}
        aria-label={$t('search_remove_reaction')}>{$t('ui_remove_c3812fc4')}</button
      >
    {/if}
  </div>
</div>
<div
  bind:this={panel}
  {id}
  popover="auto"
  class="reaction-popover"
  role="presentation"
  ontoggle={(event) => {
    open = event.newState === 'open';
    if (open) void loadCustomEmojis();
  }}
  onclick={(event) => {
    if (event.target === event.currentTarget) close();
  }}
  onkeydown={(event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      event.preventDefault();
      close();
    }
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
  }}
>
  {#if open}
    <div class="picker-content">
      {#if error}
        <div class="load-error" role="alert">
          {error}
          <button type="button" disabled={loading} onclick={() => void loadCustomEmojis()}
            >{$t('ui_try_again_d8b8392e')}</button
          >
        </div>
      {/if}
      <EmojiPicker
        inline
        customEmojis={customEmojis ?? loadedEmojis}
        onSelect={(emoji) => {
          const canonical = canonicalReactionEmoji(emoji);
          if (!canonical) return;
          value = canonical;
          close();
        }}
        onClose={close}
      />
    </div>
  {/if}
</div>

<style>
  .reaction-filter {
    display: grid;
    gap: 4px;
    align-content: start;
  }
  .reaction-filter > span {
    font-size: 0.8rem;
    font-weight: 650;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .controls button {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-height: 40px;
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface-raised);
    color: var(--text);
    cursor: pointer;
  }
  .controls button:hover {
    background: var(--surface-hover);
  }
  .choose-reaction {
    font-weight: 700;
  }
  .reaction-popover {
    position: fixed;
    inset: 0;
    width: 100%;
    height: 100dvh;
    max-width: none;
    max-height: none;
    margin: 0;
    padding: 14px;
    border: 0;
    background: rgb(0 0 0 / 25%);
    color: var(--text);
    overflow: auto;
  }
  .reaction-popover:popover-open {
    display: grid;
    place-items: center;
  }
  .picker-content {
    max-width: 100%;
  }
  .load-error {
    max-width: 420px;
    padding: 12px;
    background: var(--surface-raised);
  }
  @media (max-width: 620px) {
    .controls button {
      min-height: 44px;
    }
  }
</style>
