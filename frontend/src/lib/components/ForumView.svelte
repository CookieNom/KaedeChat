<script lang="ts">
  import { entityKey } from '$lib/chat/refs';
  import {
    filterForumPosts,
    FORUM_POST_CONTENT_MAX_LENGTH,
    forumDefaultLayout,
    forumDefaultSort,
    forumPostThumbnail,
    forumRequiresTag,
    forumTags,
    isPinnedForumPost,
    type ForumLayout,
    type ForumSortOrder
  } from '$lib/chat/threads';
  import type { Channel, Guild } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';
  import type { CustomEmojiOption } from '$lib/chat/emojis';
  import type { PendingUpload } from '$lib/media/uploads';
  import { attachmentMediaPath, authenticatedMedia } from '$lib/media/authenticated';
  import { guildChannelPath } from '$lib/navigation/routes';
  import { tick } from 'svelte';
  import EmojiPicker from './EmojiPicker.svelte';
  import Icon from './Icon.svelte';
  import ForumTagEmoji from './ForumTagEmoji.svelte';
  import UploadPreviewTray from './UploadPreviewTray.svelte';

  let {
    guild,
    forum,
    posts,
    loading = false,
    loadingMore = false,
    hasMore = false,
    error = '',
    canCreate = false,
    canManageTags = false,
    customEmojis = [],
    busy = false,
    uploads = [],
    compact = false,
    onCreate,
    onFiltersChange,
    onLoadMore,
    onFiles,
    onRemoveUpload
  }: {
    guild: Guild;
    forum: Channel;
    posts: Channel[];
    loading?: boolean;
    loadingMore?: boolean;
    hasMore?: boolean;
    error?: string;
    canCreate?: boolean;
    canManageTags?: boolean;
    customEmojis?: CustomEmojiOption[];
    busy?: boolean;
    uploads?: PendingUpload[];
    compact?: boolean;
    onCreate: (draft: {
      name: string;
      content: string;
      appliedTagIds: string[];
    }) => Promise<void> | void;
    onFiltersChange?: (filters: {
      query: string;
      selectedTagIds: string[];
      sort: ForumSortOrder;
    }) => Promise<void> | void;
    onLoadMore?: () => Promise<void> | void;
    onFiles?: (files: FileList) => Promise<void> | void;
    onRemoveUpload?: (key: string) => void;
  } = $props();

  let query = $state('');
  let selectedTags = $state<string[]>([]);
  let sort = $state<ForumSortOrder>('recent_activity');
  let layout = $state<ForumLayout>('list');
  let composerOpen = $state(false);
  let title = $state('');
  let message = $state('');
  let postTags = $state<string[]>([]);
  let guidelinesVisible = $state(true);
  let emojiPickerOpen = $state(false);
  let fileInput = $state<HTMLInputElement | null>(null);
  let messageInput = $state<HTMLTextAreaElement | null>(null);
  let sortViewMenu = $state<HTMLDetailsElement | null>(null);
  let configuredForum = '';
  let emittedFilters = '';

  const tags = $derived(forumTags(forum));
  const visiblePosts = $derived(
    filterForumPosts(posts, {
      query,
      selectedTagIds: new Set(selectedTags),
      sort
    })
  );
  const uploadReady = $derived(!uploads.some((upload) => upload.status === 'uploading'));
  const hasReadyAttachment = $derived(
    uploads.some((upload) => upload.status === 'ready' && Boolean(upload.attachmentId))
  );
  const hasStarter = $derived(Boolean(message.trim() || hasReadyAttachment));
  const requireTag = $derived(forumRequiresTag(forum));

  $effect(() => {
    const key = entityKey(forum);
    if (configuredForum === key) return;
    configuredForum = key;
    sort = forumDefaultSort(forum);
    layout = forumDefaultLayout(forum);
    query = '';
    selectedTags = [];
    composerOpen = false;
    title = '';
    message = '';
    postTags = [];
    guidelinesVisible = true;
    emojiPickerOpen = false;
    emittedFilters = JSON.stringify({ query, selectedTagIds: selectedTags, sort });
  });

  $effect(() => {
    if (!configuredForum || !onFiltersChange) return;
    const filters = { query, selectedTagIds: [...selectedTags], sort };
    const key = JSON.stringify(filters);
    if (key === emittedFilters) return;
    emittedFilters = key;
    const timer = window.setTimeout(() => void onFiltersChange(filters), 250);
    return () => window.clearTimeout(timer);
  });

  function toggle(values: string[], value: string): string[] {
    return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
  }

  function resetSortAndView() {
    sort = forumDefaultSort(forum);
    layout = forumDefaultLayout(forum);
  }

  async function submitPost() {
    if (
      !title.trim() ||
      !hasStarter ||
      message.length > FORUM_POST_CONTENT_MAX_LENGTH ||
      (requireTag && !postTags.length) ||
      busy ||
      !uploadReady
    )
      return;
    await onCreate({ name: title.trim(), content: message.trim(), appliedTagIds: postTags });
    title = '';
    message = '';
    postTags = [];
    emojiPickerOpen = false;
    composerOpen = false;
  }

  function insertEmoji(value: string) {
    const start = messageInput?.selectionStart ?? message.length;
    const end = messageInput?.selectionEnd ?? start;
    const next = `${message.slice(0, start)}${value}${message.slice(end)}`;
    if (next.length > FORUM_POST_CONTENT_MAX_LENGTH) return;
    message = next;
    emojiPickerOpen = false;
    void tick().then(() => {
      messageInput?.focus();
      messageInput?.setSelectionRange(start + value.length, start + value.length);
    });
  }

  function preview(post: Channel): string {
    const starter = post.starter_message;
    const content = starter?.e2ee
      ? starter.e2ee_verified === true
        ? (starter.decrypted_content ?? '')
        : ''
      : (starter?.content ?? '');
    return content.replace(/\s+/g, ' ').trim();
  }

  function postTagsFor(post: Channel) {
    const applied = new Set(post.applied_tag_ids ?? []);
    return tags.filter((tag) => applied.has(tag.id));
  }

  function togglePostTag(tagId: string) {
    if (postTags.includes(tagId)) postTags = postTags.filter((item) => item !== tagId);
    else if (postTags.length < 5) postTags = [...postTags, tagId];
  }

  function timestamp(post: Channel): string {
    const value =
      post.last_message?.created_at ?? post.starter_message?.created_at ?? post.archive_timestamp;
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? ''
      : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
  }

  function forumScrolled(event: Event) {
    if (!hasMore || loading || loadingMore || !onLoadMore) return;
    const target = event.currentTarget as HTMLElement;
    if (target.scrollHeight - target.scrollTop - target.clientHeight < 360) void onLoadMore();
  }

  function dismissSortViewOnOutsidePointer(event: PointerEvent) {
    const target = event.target;
    if (sortViewMenu?.open && target instanceof Node && !sortViewMenu.contains(target)) {
      sortViewMenu.open = false;
    }
  }

  function dismissSortViewOnEscape(event: KeyboardEvent) {
    if (!sortViewMenu?.open || event.key !== 'Escape') return;
    event.preventDefault();
    sortViewMenu.open = false;
    sortViewMenu.querySelector<HTMLElement>('summary')?.focus();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- guildChannelPath resolves the typed route -->
<svelte:window
  onpointerdown={dismissSortViewOnOutsidePointer}
  onkeydown={dismissSortViewOnEscape}
/>

<section
  class:compact
  class="forum-view"
  aria-label={`${forum.name ?? 'Forum'} posts`}
  onscroll={forumScrolled}
>
  {#if !composerOpen}
    <div class="forum-toolbar">
      <label class="forum-search">
        <Icon name="search" size={19} />
        <input bind:value={query} aria-label="Search post titles" placeholder="Search" />
      </label>
      <details bind:this={sortViewMenu} class="sort-view-menu">
        <summary>↕ Sort &amp; View <Icon name="chevron-down" size={15} /></summary>
        <div class="sort-view-popover">
          <fieldset>
            <legend>Sort By</legend>
            <label
              ><input type="radio" bind:group={sort} value="recent_activity" />Recently Active</label
            >
            <label><input type="radio" bind:group={sort} value="creation_date" />Date Posted</label>
          </fieldset>
          <fieldset>
            <legend>View As</legend>
            <label><input type="radio" bind:group={layout} value="list" />List</label>
            <label><input type="radio" bind:group={layout} value="gallery" />Gallery</label>
          </fieldset>
          <button type="button" onclick={resetSortAndView}>Reset to default</button>
        </div>
      </details>
      {#if canCreate}
        <button class="new-post" type="button" onclick={() => (composerOpen = true)}>
          <Icon name="message" size={17} />New Post
        </button>
      {/if}
    </div>

    {#if tags.length}
      <div class="tag-filters" aria-label="Filter by tags">
        {#each tags as tag (tag.id)}
          <button
            class:active={selectedTags.includes(tag.id)}
            type="button"
            aria-pressed={selectedTags.includes(tag.id)}
            onclick={() => (selectedTags = toggle(selectedTags, tag.id))}
          >
            <ForumTagEmoji
              {tag}
              guildId={forum.guild_id}
              guildDomain={forum.guild_domain}
              {customEmojis}
            />
            {tag.name}
          </button>
        {/each}
      </div>
    {/if}
  {/if}

  {#if composerOpen}
    <form
      class="forum-composer"
      onsubmit={(event) => {
        event.preventDefault();
        void submitPost();
      }}
    >
      <div class="post-fields">
        <button
          class="close-composer"
          type="button"
          disabled={busy}
          aria-label="Close new post"
          onclick={() => {
            emojiPickerOpen = false;
            composerOpen = false;
          }}
        >
          <Icon name="x" size={21} />
        </button>
        <div class="post-copy-fields">
          <input
            bind:value={title}
            maxlength="100"
            required
            placeholder="Title"
            aria-label="Post title"
          />
          <textarea
            bind:this={messageInput}
            bind:value={message}
            maxlength={FORUM_POST_CONTENT_MAX_LENGTH}
            rows="5"
            placeholder="Enter a message…"
            aria-label="Post message"
          ></textarea>
        </div>
        <input
          bind:this={fileInput}
          class="visually-hidden"
          type="file"
          multiple
          onchange={(event) => {
            if (event.currentTarget.files) void onFiles?.(event.currentTarget.files);
            event.currentTarget.value = '';
          }}
        />
        <button
          class="attach-post"
          type="button"
          disabled={!onFiles || busy}
          aria-label="Add images or files"
          title="Add images or files"
          onclick={() => fileInput?.click()}
        >
          <Icon name="image-plus" size={34} />
        </button>
      </div>
      {#if uploads.length && onRemoveUpload}
        <div class="composer-uploads">
          <UploadPreviewTray {uploads} onRemove={onRemoveUpload} />
        </div>
      {/if}
      {#if tags.length}
        <div class="post-tags" aria-label="Post tags">
          {#each tags as tag (tag.id)}
            <label
              class:active={postTags.includes(tag.id)}
              class:disabled={tag.moderated && !canManageTags}
            >
              <input
                type="checkbox"
                checked={postTags.includes(tag.id)}
                disabled={(tag.moderated && !canManageTags) ||
                  (!postTags.includes(tag.id) && postTags.length >= 5)}
                onchange={() => togglePostTag(tag.id)}
              />
              <ForumTagEmoji
                {tag}
                guildId={forum.guild_id}
                guildDomain={forum.guild_domain}
                {customEmojis}
              />
              {tag.name}{tag.moderated ? ' · Moderators' : ''}
            </label>
          {/each}
          {#if requireTag && !postTags.length}<small>Select at least one tag.</small>{/if}
        </div>
      {/if}
      <footer>
        <div class="forum-emoji-control">
          <button
            class:active={emojiPickerOpen}
            type="button"
            disabled={busy}
            aria-label="Choose an emoji"
            aria-expanded={emojiPickerOpen}
            onclick={() => (emojiPickerOpen = !emojiPickerOpen)}>☺</button
          >
          {#if emojiPickerOpen}
            <EmojiPicker
              {customEmojis}
              onSelect={insertEmoji}
              onClose={() => (emojiPickerOpen = false)}
            />
          {/if}
        </div>
        <span></span>
        {#if forum.topic}
          <button
            class:active={guidelinesVisible}
            class="guidelines-toggle"
            type="button"
            aria-label={guidelinesVisible ? 'Hide post guidelines' : 'Show post guidelines'}
            aria-pressed={guidelinesVisible}
            onclick={() => (guidelinesVisible = !guidelinesVisible)}
          >
            <Icon name="check" size={18} />
          </button>
        {/if}
        <button
          class="submit-post"
          disabled={busy ||
            !title.trim() ||
            !hasStarter ||
            (requireTag && !postTags.length) ||
            !uploadReady}
        >
          {busy ? 'Posting…' : 'Post'}
        </button>
      </footer>
    </form>
    {#if forum.topic && guidelinesVisible}
      <aside class="post-guidelines">
        <div>
          <span><Icon name="check" size={18} /><strong>Post Guidelines</strong></span>
          <button
            type="button"
            aria-label="Hide post guidelines"
            onclick={() => (guidelinesVisible = false)}
          >
            <Icon name="x" size={17} />
          </button>
        </div>
        <p>{forum.topic}</p>
      </aside>
    {/if}
  {/if}

  {#if error}<p class="forum-error" role="alert">{error}</p>{/if}
  {#if loading}
    <div class="forum-empty" role="status">Loading posts…</div>
  {:else if !visiblePosts.length}
    <div class="forum-empty">
      <Icon name="message" size={30} />
      <strong>{posts.length ? 'No posts match those filters' : 'There are no posts yet'}</strong>
      {#if canCreate && !posts.length}<span>Be the first to start a conversation.</span>{/if}
    </div>
  {:else}
    <div class:gallery={layout === 'gallery'} class="forum-posts">
      {#each visiblePosts as post (entityKey(post))}
        {@const image = forumPostThumbnail(post)}
        <a class:pinned={isPinnedForumPost(post)} href={guildChannelPath(guild, post)}>
          <div class="post-copy">
            <div class="post-title">
              {#if isPinnedForumPost(post)}<span title="Pinned post">📌</span>{/if}
              <strong>{post.name}</strong>
            </div>
            {#if post.starter_message?.author || preview(post)}
              <p>
                {#if post.starter_message?.author}<strong
                    >{userDisplayName(post.starter_message.author)}:</strong
                  >{/if}
                {preview(post)}
              </p>
            {/if}
            {#if postTagsFor(post).length}
              <div class="post-tag-list">
                {#each postTagsFor(post) as tag (tag.id)}
                  <span>
                    <ForumTagEmoji
                      {tag}
                      guildId={forum.guild_id}
                      guildDomain={forum.guild_domain}
                      {customEmojis}
                    />
                    {tag.name}
                  </span>
                {/each}
              </div>
            {/if}
            <footer>
              <span>💬 {post.message_count ?? 0}</span>
              <time>{timestamp(post)}</time>
            </footer>
          </div>
          {#if image}
            <img
              use:authenticatedMedia={{
                path: attachmentMediaPath(
                  image.origin_domain,
                  image.id,
                  layout === 'gallery' ? 'thumbnail_512' : 'thumbnail_128',
                  image.history_media_url
                ),
                contentType: image.content_type
              }}
              src=""
              alt={image.filename}
              loading="lazy"
              decoding="async"
            />
          {/if}
        </a>
      {/each}
    </div>
  {/if}
  {#if loadingMore}<div class="forum-loading-more" role="status">Loading posts…</div>{/if}
</section>

<style>
  .forum-view {
    min-width: 0;
    min-height: 0;
    overflow: auto;
    padding: 1rem clamp(0.8rem, 2vw, 1.5rem) 1.5rem;
    background: var(--surface);
  }

  .forum-view.compact {
    border-right: 1px solid var(--line);
    padding: 0.75rem;
  }

  .forum-toolbar {
    position: sticky;
    z-index: 4;
    top: -1rem;
    display: grid;
    grid-template-columns: minmax(12rem, 1fr) auto;
    gap: 0.65rem;
    padding: 0.8rem 0;
    background: var(--surface);
  }

  .forum-search {
    grid-column: 1;
    grid-row: 1;
  }

  .sort-view-menu {
    grid-column: 1 / -1;
    grid-row: 2;
    width: fit-content;
  }

  .new-post {
    grid-column: 2;
    grid-row: 1;
  }

  .forum-search {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0 0.75rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
  }

  .forum-search input {
    width: 100%;
    min-height: 42px;
    border: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    outline: 0;
  }

  .sort-view-menu {
    position: relative;
  }

  .sort-view-menu summary,
  .new-post,
  .tag-filters button {
    display: inline-flex;
    min-height: 42px;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0 0.8rem;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-size: 0.76rem;
    font-weight: 750;
    cursor: pointer;
    list-style: none;
  }

  .sort-view-menu summary::-webkit-details-marker {
    display: none;
  }

  .new-post {
    border-color: var(--accent);
    color: var(--on-accent);
    background: var(--accent);
  }

  .sort-view-popover {
    position: absolute;
    z-index: 10;
    top: calc(100% + 0.35rem);
    right: 0;
    display: grid;
    width: 230px;
    gap: 0.65rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.75rem;
    background: var(--surface-raised);
    box-shadow: var(--shadow-md);
  }

  fieldset {
    display: grid;
    gap: 0.35rem;
    margin: 0;
    border: 0;
    padding: 0;
  }

  legend {
    margin-bottom: 0.25rem;
    color: var(--text-muted);
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  fieldset label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-soft);
    font-size: 0.76rem;
  }

  .sort-view-popover > button {
    min-height: 34px;
    border: 0;
    border-radius: 7px;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-weight: 750;
  }

  .tag-filters,
  .post-tags,
  .post-tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .tag-filters {
    margin: 0 0 0.8rem;
  }

  .tag-filters button {
    min-height: 32px;
    padding: 0 0.65rem;
  }

  .tag-filters button.active,
  .post-tags label.active {
    border-color: var(--accent);
    color: var(--accent-text);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-subtle));
  }

  .forum-composer {
    position: relative;
    display: grid;
    margin-bottom: 1rem;
    overflow: visible;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface-subtle);
  }

  .post-fields {
    display: grid;
    min-height: 250px;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.65rem;
    padding: 0.9rem;
  }

  .post-copy-fields {
    display: grid;
    min-width: 0;
    align-self: stretch;
    grid-template-rows: auto 1fr;
  }

  .post-copy-fields input,
  .post-copy-fields textarea {
    border: 0;
    border-radius: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    outline: 0;
  }

  .post-copy-fields input {
    min-height: 34px;
    font-size: 1.05rem;
    font-weight: 750;
  }

  .post-copy-fields textarea {
    min-height: 170px;
    padding-top: 0.2rem;
    resize: none;
    line-height: 1.45;
  }

  .close-composer {
    display: grid;
    width: 28px;
    height: 34px;
    place-items: center;
    border: 0;
    padding: 0;
    color: var(--text-muted);
    background: transparent;
    cursor: pointer;
  }

  .post-fields > .attach-post {
    display: grid;
    width: 104px;
    height: 104px;
    place-items: center;
    border: 1px solid var(--line);
    border-radius: 13px;
    color: var(--text-muted);
    background: var(--surface-raised);
    cursor: pointer;
  }

  .post-fields > .attach-post:hover:not(:disabled),
  .post-fields > .attach-post:focus-visible {
    color: var(--text-soft);
    background: var(--surface-hover);
  }

  .post-fields > .attach-post:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .composer-uploads {
    border-top: 1px solid var(--line);
    padding: 0 0.85rem 0.7rem;
  }

  .post-guidelines {
    margin: -0.35rem 0 1rem;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.85rem;
    background: var(--surface-subtle);
  }

  .post-guidelines > div,
  .post-guidelines > div > span {
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }

  .post-guidelines > div {
    justify-content: space-between;
  }

  .post-guidelines button {
    display: grid;
    width: 28px;
    height: 28px;
    place-items: center;
    border: 0;
    color: var(--text-muted);
    background: transparent;
    cursor: pointer;
  }

  .post-guidelines p {
    margin: 0.45rem 0 0;
    color: var(--text-muted);
    font-size: 0.78rem;
    white-space: pre-wrap;
  }

  .post-tags {
    padding: 0 0.9rem 0.85rem 3.6rem;
  }

  .post-tags label,
  .post-tag-list span {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.28rem 0.55rem;
    color: var(--text-muted);
    background: var(--surface-raised);
    font-size: 0.68rem;
    cursor: pointer;
  }

  .post-tags label.disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .post-tags input {
    position: absolute;
    opacity: 0;
  }

  .forum-composer > footer {
    display: grid;
    grid-template-columns: auto 1fr auto auto;
    align-items: center;
    gap: 0.5rem;
    border-top: 1px solid var(--line);
    padding: 0.7rem 0.85rem;
  }

  .forum-composer footer button {
    min-height: 34px;
    border: 0;
    border-radius: 7px;
    padding: 0 0.7rem;
    color: var(--text-soft);
    background: transparent;
    font-weight: 750;
  }

  .forum-composer footer .guidelines-toggle {
    width: 38px;
    padding: 0;
    background: var(--surface-raised);
  }

  .forum-composer footer .guidelines-toggle.active {
    color: var(--accent-text);
    background: color-mix(in srgb, var(--accent) 15%, var(--surface-raised));
  }

  .forum-emoji-control {
    position: relative;
  }

  .forum-emoji-control > button {
    width: 38px;
    padding: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.2rem;
  }

  .forum-emoji-control > button:hover,
  .forum-emoji-control > button.active {
    color: var(--text);
    background: var(--surface-hover);
  }

  .forum-emoji-control :global(.emoji-picker) {
    right: auto;
    left: 0;
    bottom: calc(100% + 10px);
  }

  .forum-composer footer .submit-post {
    color: var(--on-accent);
    background: var(--accent);
  }

  .forum-error,
  .forum-empty,
  .forum-loading-more {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 1rem;
  }

  .forum-error {
    color: var(--danger);
    background: var(--danger-soft);
  }

  .forum-empty {
    display: grid;
    min-height: 180px;
    place-content: center;
    justify-items: center;
    gap: 0.35rem;
    color: var(--text-muted);
    text-align: center;
  }

  .forum-loading-more {
    margin-top: 0.7rem;
    color: var(--text-muted);
    text-align: center;
    font-size: 0.72rem;
  }

  .forum-posts {
    display: grid;
    gap: 0.65rem;
  }

  .forum-posts.gallery {
    grid-template-columns: repeat(auto-fill, minmax(min(250px, 100%), 1fr));
  }

  .forum-posts > a {
    display: flex;
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 11px;
    color: var(--text);
    background: var(--surface-subtle);
    text-decoration: none;
    transition:
      border-color 120ms ease,
      background-color 120ms ease;
  }

  .forum-posts > a:hover,
  .forum-posts > a:focus-visible {
    border-color: color-mix(in srgb, var(--text-muted) 52%, var(--line));
    background: var(--surface-hover);
  }

  .forum-posts > a.pinned {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
  }

  .forum-posts.gallery > a {
    flex-direction: column;
  }

  .forum-posts img {
    width: 104px;
    height: 104px;
    flex: 0 0 104px;
    align-self: center;
    margin: 0.75rem 0.75rem 0.75rem 0;
    border-radius: 8px;
    object-fit: cover;
    background: var(--surface-raised);
  }

  .forum-posts.gallery img {
    order: -1;
    width: 100%;
    height: 150px;
    flex-basis: auto;
    align-self: stretch;
    margin: 0;
    border-radius: 0;
  }

  .post-copy {
    display: grid;
    min-width: 0;
    flex: 1;
    gap: 0.4rem;
    padding: 0.85rem;
  }

  .post-title,
  .post-copy > footer {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.45rem;
  }

  .post-title strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .post-copy p {
    display: -webkit-box;
    overflow: hidden;
    margin: 0;
    color: var(--text-muted);
    font-size: 0.78rem;
    line-height: 1.4;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .post-copy > footer {
    justify-content: flex-start;
    margin-top: 0.15rem;
    color: var(--text-muted);
    font-size: 0.66rem;
  }

  .post-copy > footer time {
    margin-left: auto;
  }

  @media (max-width: 700px) {
    .forum-toolbar {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .sort-view-menu {
      grid-column: 1;
      grid-row: 2;
      justify-self: start;
    }

    .new-post {
      grid-column: 2;
      grid-row: 1;
    }

    .forum-posts.gallery {
      grid-template-columns: 1fr;
    }

    .forum-posts:not(.gallery) img {
      width: 76px;
      height: 76px;
      flex-basis: 76px;
      margin: 0.65rem 0.65rem 0.65rem 0;
    }

    .post-fields {
      min-height: 220px;
      grid-template-columns: auto minmax(0, 1fr) 76px;
      padding: 0.75rem;
    }

    .post-fields > .attach-post {
      width: 76px;
      height: 76px;
    }

    .post-tags {
      padding-left: 3.2rem;
    }
  }
</style>
