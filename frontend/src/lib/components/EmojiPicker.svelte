<script lang="ts">
  import { t } from '$lib/ui/locale';

  import {
    emojiCategories,
    groupCustomEmojis,
    loadUnicodeEmojis,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import type { StickerOption } from '$lib/chat/stickers';
  import { onMount, tick } from 'svelte';
  import { assetUrl } from '$lib/media/assets';

  let {
    inline = false,
    customEmojis = [],
    stickers = [],
    onSelect,
    onStickerSelect,
    onClose
  }: {
    inline?: boolean;
    customEmojis?: CustomEmojiOption[];
    stickers?: StickerOption[];
    onSelect: (value: string) => void;
    onStickerSelect?: (sticker: StickerOption) => void;
    onClose: () => void;
  } = $props();
  let mode = $state<'emoji' | 'sticker'>('emoji');
  let query = $state('');
  let stickerQuery = $state('');
  let activeSection = $state('people');
  let failedIcons = $state<string[]>([]);
  let results = $state<HTMLDivElement | null>(null);
  let preview = $state<{ value: string; name: string; url?: string } | null>(null);
  let searchInput = $state<HTMLInputElement | null>(null);
  let stickerSearchInput = $state<HTMLInputElement | null>(null);
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let loading = $state(true);
  let loadFailed = $state(false);
  const normalizedQuery = $derived(query.trim().toLowerCase());
  const matchingUnicode = $derived(
    unicodeEmojis.filter(
      (emoji) =>
        !normalizedQuery ||
        emoji.name.includes(normalizedQuery) ||
        emoji.keywords.some((keyword) => keyword.includes(normalizedQuery))
    )
  );
  const unicodeGroups = $derived(
    emojiCategories
      .map((category) => ({
        ...category,
        emojis: matchingUnicode.filter((emoji) => emoji.category === category.id)
      }))
      .filter((group) => group.emojis.length)
  );
  const allCustomGroups = $derived(groupCustomEmojis(customEmojis));
  const matchingCustom = $derived(
    customEmojis.filter(
      (emoji) =>
        !normalizedQuery ||
        emoji.name.toLowerCase().includes(normalizedQuery) ||
        emoji.guild_name?.toLowerCase().includes(normalizedQuery)
    )
  );
  const customEmojiGroups = $derived(groupCustomEmojis(matchingCustom));

  function syncSection() {
    if (!results || normalizedQuery) return;
    const top = results.getBoundingClientRect().top;
    const sections = [...results.querySelectorAll<HTMLElement>('[data-section]')];
    const current = sections.find((section) => section.getBoundingClientRect().bottom > top + 36);
    if (current) activeSection = current.dataset.section!;
  }

  async function jumpTo(key: string) {
    query = '';
    await tick();
    const section = [...(results?.querySelectorAll<HTMLElement>('[data-section]') ?? [])].find(
      (section) => section.dataset.section === key
    );
    if (section && results) {
      results.scrollTop +=
        section.getBoundingClientRect().top - results.getBoundingClientRect().top;
      activeSection = key;
    }
  }

  $effect(() => {
    activeSection = customEmojiGroups[0]?.key ?? 'people';
    if (results) results.scrollTop = 0;
  });
  const matchingStickers = $derived(
    stickers.filter((sticker) => {
      const needle = stickerQuery.trim().toLowerCase();
      return (
        !needle ||
        sticker.name.toLowerCase().includes(needle) ||
        sticker.guild_name?.toLowerCase().includes(needle) ||
        sticker.description?.toLowerCase().includes(needle)
      );
    })
  );
  const stickerGroups = $derived.by(() => {
    const result: Array<{ key: string; name: string; stickers: StickerOption[] }> = [];
    for (const sticker of matchingStickers) {
      const key = `${sticker.guild_id}@${sticker.guild_domain}`;
      let group = result.find((item) => item.key === key);
      if (!group) {
        group = { key, name: sticker.guild_name ?? 'Guild stickers', stickers: [] };
        result.push(group);
      }
      group.stickers.push(sticker);
    }
    return result;
  });

  function selectMode(next: 'emoji' | 'sticker') {
    mode = next;
    void Promise.resolve().then(() => {
      if (next === 'emoji') searchInput?.focus();
      else stickerSearchInput?.focus();
    });
  }

  async function loadEmoji() {
    loading = true;
    loadFailed = false;
    try {
      unicodeEmojis = await loadUnicodeEmojis();
    } catch {
      unicodeEmojis = [];
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    searchInput?.focus();
    void loadEmoji();
  });
</script>

<div
  class="emoji-picker"
  class:inline
  role="dialog"
  aria-modal="false"
  aria-label={onStickerSelect
    ? $t('ui_choose_an_emoji_or_sticker_b4c5df44')
    : $t('ui_choose_an_emoji_54bc3777')}
>
  <header>
    <div class="expression-tabs" role="tablist" aria-label={$t('ui_expression_type_3a6be3a1')}>
      <button
        class:active={mode === 'emoji'}
        type="button"
        role="tab"
        aria-selected={mode === 'emoji'}
        onclick={() => selectMode('emoji')}>{$t('ui_emoji_61ad8976')}</button
      >
      {#if onStickerSelect}
        <button
          class:active={mode === 'sticker'}
          type="button"
          role="tab"
          aria-selected={mode === 'sticker'}
          onclick={() => selectMode('sticker')}>{$t('ui_stickers_dbf9cbbe')}</button
        >
      {/if}
    </div>
    <button
      class="icon-button"
      type="button"
      onclick={onClose}
      aria-label={$t('ui_close_expression_picker_616cb474')}>×</button
    >
  </header>
  {#if mode === 'emoji'}
    <label class="emoji-search">
      <span class="visually-hidden">{$t('ui_search_emoji_87fafa72')}</span>
      <input
        bind:this={searchInput}
        bind:value={query}
        placeholder={$t('ui_search_emoji_87fafa72')}
      />
    </label>
    <div class="emoji-browser">
      <nav aria-label={$t('ui_emoji_categories_fed48f97')}>
        {#each allCustomGroups as group (group.key)}
          <button
            class="server-shortcut"
            class:active={!normalizedQuery && activeSection === group.key}
            aria-pressed={!normalizedQuery && activeSection === group.key}
            type="button"
            title={group.name}
            aria-label={group.name}
            onclick={() => jumpTo(group.key)}
          >
            {#if group.emojis[0].guild_icon_hash && !failedIcons.includes(group.key)}
              <img
                src={assetUrl(
                  group.emojis[0].guild_icon_hash,
                  'thumbnail_128',
                  group.emojis[0].guild_domain
                )}
                alt=""
                onerror={() => (failedIcons = [...failedIcons, group.key])}
              />
            {:else}
              <span>{group.name.slice(0, 2).toUpperCase()}</span>
            {/if}
          </button>
        {/each}
        {#if allCustomGroups.length}<div class="nav-divider"></div>{/if}
        {#each emojiCategories as item (item.id)}
          <button
            class:active={!normalizedQuery && activeSection === item.id}
            aria-pressed={!normalizedQuery && activeSection === item.id}
            disabled={loading || loadFailed}
            type="button"
            title={item.label}
            aria-label={item.label}
            onclick={() => jumpTo(item.id)}>{item.icon}</button
          >
        {/each}
      </nav>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Scrollable results need keyboard access.) -->
      <div
        class="emoji-results"
        bind:this={results}
        onscroll={syncSection}
        role="region"
        aria-label={$t('ui_emoji_results_de396e53')}
        tabindex="0"
      >
        {#each customEmojiGroups as group (group.key)}
          <section class="custom-emoji-group" data-section={group.key}>
            <h3>{group.name}</h3>
            <div class="emoji-grid custom-emojis">
              {#each group.emojis as emoji (`${emoji.id}@${emoji.origin_domain}`)}
                <button
                  type="button"
                  title={`:${emoji.name}: — ${group.name}`}
                  onmouseenter={() => (preview = emoji)}
                  onfocus={() => (preview = emoji)}
                  onclick={() => onSelect(emoji.value)}
                >
                  <img src={emoji.url} alt={`:${emoji.name}:`} loading="lazy" />
                </button>
              {/each}
            </div>
          </section>
        {/each}
        {#if loading}
          <p role="status">{$t('ui_loading_emoji_f35e9103')}</p>
        {:else if loadFailed}
          <div role="alert">
            <p class="form-error">
              {$t('ui_could_not_load_emoji_data_check_your_connecti_2b4497a8')}
            </p>
            <button class="show-more" type="button" onclick={() => void loadEmoji()}
              >{$t('ui_try_again_d8b8392e')}</button
            >
          </div>
        {:else}
          {#each unicodeGroups as group (group.id)}
            <section data-section={group.id}>
              <h3>{group.label}</h3>
              <div class="emoji-grid">
                {#each group.emojis as emoji (emoji.value)}
                  <button
                    type="button"
                    title={emoji.name}
                    onmouseenter={() => (preview = emoji)}
                    onfocus={() => (preview = emoji)}
                    onclick={() => onSelect(emoji.value)}>{emoji.value}</button
                  >
                {/each}
              </div>
            </section>
          {/each}
        {/if}
        {#if !loading && !loadFailed && !matchingCustom.length && !matchingUnicode.length}
          <p role="status">{$t('ui_no_emoji_found_d2cab146')}</p>
        {/if}
      </div>
    </div>
    <footer>
      <span aria-hidden="true"
        >{#if preview?.url}<img src={preview.url} alt="" />{:else}{preview?.value ??
            '😀'}{/if}</span
      >
      <small>{preview?.name ?? $t('ui_choose_an_emoji_54bc3777')}</small>
    </footer>
  {:else}
    <label class="sticker-search">
      <span class="visually-hidden">{$t('ui_search_stickers_5c55ffd0')}</span>
      <input
        bind:this={stickerSearchInput}
        bind:value={stickerQuery}
        placeholder={$t('ui_search_stickers_5c55ffd0')}
      />
    </label>
    <div
      class="sticker-results"
      role="region"
      aria-label={$t('ui_sticker_results_a6e7d609')}
      aria-live="polite"
    >
      {#each stickerGroups as group (group.key)}
        <section>
          <h3>{group.name}</h3>
          <div class="sticker-grid">
            {#each group.stickers as sticker (`${sticker.id}@${sticker.origin_domain}`)}
              <button
                type="button"
                title={sticker.description ?? sticker.name}
                onclick={() => onStickerSelect?.(sticker)}
              >
                <img src={sticker.url} alt={sticker.name} loading="lazy" />
                <span>{sticker.name}</span>
              </button>
            {/each}
          </div>
        </section>
      {:else}
        <p>
          {stickers.length
            ? $t('ui_no_stickers_found_35311c55')
            : $t('ui_no_stickers_are_available_yet_e9d0a717')}
        </p>
      {/each}
    </div>
  {/if}
</div>

<style>
  .emoji-picker {
    position: absolute;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 25;
    display: flex;
    flex-direction: column;
    width: min(420px, calc(100vw - 28px));
    height: min(520px, 65dvh);
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: var(--surface-raised);
    box-shadow: 0 22px 55px rgb(0 0 0 / 38%);
  }
  .emoji-picker.inline {
    position: static;
    width: min(420px, calc(100vw - 28px));
    height: min(520px, 65dvh);
  }
  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
  }
  header {
    border-bottom: 1px solid var(--line-soft);
  }
  .expression-tabs {
    display: flex;
    gap: 4px;
  }
  .expression-tabs button {
    padding: 8px 13px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--text-muted);
    font-weight: 750;
  }
  .expression-tabs button:hover,
  .expression-tabs button.active {
    background: var(--surface-hover);
    color: var(--text);
  }
  .emoji-search {
    padding: 12px 14px 8px;
  }
  .emoji-search input {
    width: 100%;
  }
  .sticker-search {
    padding: 12px 14px 8px;
  }
  .sticker-search input {
    width: 100%;
  }
  .emoji-browser {
    display: flex;
    flex: 1;
    min-height: 0;
    min-width: 0;
    border-top: 1px solid var(--line-soft);
  }
  nav {
    display: flex;
    flex-direction: column;
    flex: 0 0 56px;
    align-items: center;
    gap: 6px;
    overflow-x: hidden;
    overflow-y: auto;
    padding: 8px 4px;
    border-right: 1px solid var(--line-soft);
    scrollbar-width: none;
    overscroll-behavior: contain;
  }
  nav::-webkit-scrollbar {
    display: none;
  }
  nav button {
    display: grid;
    flex-shrink: 0;
    width: 44px;
    height: 44px;
    padding: 4px;
    place-items: center;
    border: 1px solid transparent;
    border-radius: 12px;
    background: transparent;
    color: var(--text-muted);
    font-size: 1.35rem;
    cursor: pointer;
  }
  nav button:hover,
  nav button.active {
    background: var(--surface-hover);
    color: var(--text);
  }
  nav button.active {
    border-color: var(--accent);
  }
  nav button:disabled {
    opacity: 0.4;
    cursor: default;
  }
  nav .server-shortcut {
    background: var(--surface-hover);
    font-size: 0.8rem;
    font-weight: 750;
    overflow: hidden;
  }
  nav img {
    width: 34px;
    height: 34px;
    border-radius: 9px;
    object-fit: cover;
  }
  .nav-divider {
    flex-shrink: 0;
    width: 28px;
    height: 1px;
    background: var(--line);
    margin: 2px;
  }
  button:focus-visible,
  .emoji-results:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .emoji-results {
    flex: 1;
    min-height: 0;
    min-width: 0;
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 0 10px 12px;
    scrollbar-width: thin;
    scrollbar-gutter: stable;
    touch-action: pan-y;
    -webkit-overflow-scrolling: touch;
  }
  .emoji-results h3 {
    position: sticky;
    top: 0;
    z-index: 1;
    padding: 12px 0 8px;
    margin: 0;
    overflow-wrap: anywhere;
    background: var(--surface-raised);
    color: var(--text-muted);
    font-size: 0.72rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }
  .emoji-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(36px, 1fr));
    gap: 3px;
  }
  .emoji-grid button {
    display: grid;
    aspect-ratio: 1;
    padding: 0;
    overflow: hidden;
    min-width: 0;
    place-items: center;
    border: 0;
    border-radius: 9px;
    background: transparent;
    font-size: 1.55rem;
  }
  .emoji-grid button:hover {
    background: var(--surface-hover);
    transform: scale(1.08);
  }
  .emoji-results section {
    padding-bottom: 14px;
  }
  .emoji-results section:last-child {
    min-height: 100%;
  }
  .custom-emojis img {
    width: 30px;
    height: 30px;
    object-fit: contain;
  }
  .emoji-results p {
    color: var(--text-muted);
    text-align: center;
  }
  .show-more {
    display: block;
    margin: 10px auto 2px;
    border: 0;
    background: transparent;
    color: var(--accent);
    font-weight: 750;
  }
  footer {
    justify-content: flex-start;
    gap: 10px;
    border-top: 1px solid var(--line-soft);
  }
  footer > span {
    font-size: 1.6rem;
  }
  footer img {
    width: 30px;
    height: 30px;
    object-fit: contain;
  }
  footer small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
    text-transform: capitalize;
  }
  .sticker-results {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 8px 14px 14px;
    overscroll-behavior: contain;
  }
  .sticker-results h3 {
    margin: 10px 0 8px;
    color: var(--text-muted);
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .sticker-results > p {
    color: var(--text-muted);
    text-align: center;
  }
  .sticker-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }
  .sticker-grid button {
    display: grid;
    gap: 3px;
    min-width: 0;
    padding: 7px;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--text-muted);
    font-size: 0.7rem;
  }
  .sticker-grid button:hover {
    background: var(--surface-hover);
    color: var(--text);
  }
  .sticker-grid img {
    width: 100%;
    aspect-ratio: 1;
    object-fit: contain;
  }
  .sticker-grid span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  @media (max-width: 620px) {
    .emoji-browser {
      flex-direction: column;
    }
    nav {
      order: 1;
      flex: 0 0 auto;
      flex-direction: row;
      overflow-x: auto;
      overflow-y: hidden;
      padding: 6px;
      border-right: 0;
      border-top: 1px solid var(--line-soft);
    }
    .nav-divider {
      width: 1px;
      height: 28px;
    }
    .emoji-grid {
      grid-template-columns: repeat(auto-fill, minmax(40px, 1fr));
    }
    footer {
      display: none;
    }
    .emoji-picker {
      position: fixed;
      right: 8px;
      bottom: 82px;
      left: 8px;
      width: auto;
      height: min(520px, calc(100dvh - 98px));
    }
  }
</style>
