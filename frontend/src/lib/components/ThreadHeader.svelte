<script lang="ts">
  import { entityKey } from '$lib/chat/refs';
  import type { CustomEmojiOption } from '$lib/chat/emojis';
  import { isForumChannel, isPinnedForumPost } from '$lib/chat/threads';
  import type {
    Channel,
    ForumTag,
    Guild,
    GuildMemberSummary,
    Message,
    ThreadMember
  } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';
  import { guildChannelPath } from '$lib/navigation/routes';
  import Icon from './Icon.svelte';
  import ForumTagEmoji from './ForumTagEmoji.svelte';
  import ReactionEmoji from './ReactionEmoji.svelte';

  let {
    guild,
    thread,
    parent,
    joined = false,
    notificationLevel = 'inherit',
    starterMessage = null,
    reactionEmoji = '👍',
    canReact = false,
    canEdit = false,
    canManage = false,
    canInviteMembers = false,
    canRemoveMembers = false,
    canEnableEncryption = false,
    canRekeyEncryption = false,
    busy = false,
    encryptionStatus = '',
    threadMembers = [],
    guildMembers = [],
    availableTags = [],
    customEmojis = [],
    onMembership,
    onNotifications,
    onReaction,
    onRename,
    onEncryption,
    onInvitable,
    onArchive,
    onLock,
    onMemberChange,
    onPin,
    onTagsChange,
    onDelete
  }: {
    guild: Guild;
    thread: Channel;
    parent: Channel | null;
    joined?: boolean;
    notificationLevel?: ThreadMember['notification_level'];
    starterMessage?: Message | null;
    reactionEmoji?: string;
    canReact?: boolean;
    canEdit?: boolean;
    canManage?: boolean;
    canInviteMembers?: boolean;
    canRemoveMembers?: boolean;
    canEnableEncryption?: boolean;
    canRekeyEncryption?: boolean;
    busy?: boolean;
    encryptionStatus?: string;
    threadMembers?: ThreadMember[];
    guildMembers?: GuildMemberSummary[];
    availableTags?: ForumTag[];
    customEmojis?: CustomEmojiOption[];
    onMembership: (joined: boolean) => Promise<void> | void;
    onNotifications: (
      level: NonNullable<ThreadMember['notification_level']>
    ) => Promise<void> | void;
    onReaction: (message: Message, emoji: string, remove: boolean) => Promise<void> | void;
    onRename: (name: string) => Promise<boolean | void> | boolean | void;
    onEncryption: () => Promise<void> | void;
    onInvitable: (invitable: boolean) => Promise<void> | void;
    onArchive: (archived: boolean) => Promise<void> | void;
    onLock: (locked: boolean) => Promise<void> | void;
    onMemberChange: (userRef: string, joined: boolean) => Promise<void> | void;
    onPin: (pinned: boolean) => Promise<void> | void;
    onTagsChange: (tagIds: string[]) => Promise<void> | void;
    onDelete: () => Promise<void> | void;
  } = $props();

  let memberQuery = $state('');
  let renameOpen = $state(false);
  let renameName = $state('');
  const forumPost = $derived(isForumChannel(parent));
  const notificationOptions: Array<{
    value: NonNullable<ThreadMember['notification_level']>;
    label: string;
  }> = [
    { value: 'inherit', label: 'Use Default' },
    { value: 'all', label: 'All Messages' },
    { value: 'mentions', label: 'Only @mentions' },
    { value: 'none', label: 'Nothing' }
  ];
  const memberKeys = $derived(
    new Set(
      threadMembers.flatMap((member) => {
        if (member.user) return [entityKey(member.user)];
        if (member.user_id && member.user_domain)
          return [`${member.user_id}@${member.user_domain}`];
        return [];
      })
    )
  );
  const visibleGuildMembers = $derived(
    guildMembers
      .filter((member) =>
        userDisplayName(member.user).toLocaleLowerCase().includes(memberQuery.toLocaleLowerCase())
      )
      .slice(0, 50)
  );

  function toggleTag(tagId: string) {
    const current = thread.applied_tag_ids ?? [];
    const next = current.includes(tagId)
      ? current.filter((item) => item !== tagId)
      : current.length < 5
        ? [...current, tagId]
        : current;
    if (next !== current) void onTagsChange(next);
  }

  function reactToPost() {
    if (!starterMessage || !reactionEmoji || thread.archived || !canReact) return;
    void onReaction(
      starterMessage,
      reactionEmoji,
      Boolean(starterMessage.reacted_emoji?.includes(reactionEmoji))
    );
  }

  function startRename() {
    if (!canEdit || thread.archived) return;
    renameName = thread.name ?? '';
    renameOpen = true;
  }

  async function submitRename() {
    const name = renameName.trim();
    if (!name || name === thread.name || busy) return;
    if ((await onRename(name)) !== false) renameOpen = false;
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- guildChannelPath resolves the typed route -->
<div class="thread-detail-header">
  <div class="thread-header">
    <div class="thread-heading">
      <div class="thread-breadcrumb">
        {#if parent}<a href={guildChannelPath(guild, parent)}>{parent.name ?? 'channel'}</a><span
            >›</span
          >{/if}
        <span>{thread.name}</span>
      </div>
      <div class="thread-title">
        <span aria-hidden="true">🧵</span>
        <strong>{thread.name}</strong>
        {#if thread.archived}<small>Archived</small>{/if}
        {#if thread.locked}<small>{forumPost ? 'Closed' : 'Locked'}</small>{/if}
      </div>
    </div>
    <div class="thread-actions">
      <button
        type="button"
        disabled={busy || thread.archived}
        title={thread.archived ? 'Archived threads cannot be joined or followed' : undefined}
        onclick={() => void onMembership(!joined)}
      >
        <Icon name={joined ? 'check' : 'bell'} size={16} />
        {forumPost ? (joined ? 'Following' : 'Follow') : joined ? 'Leave' : 'Join'}
      </button>
      {#if joined && !thread.archived}
        <details>
          <summary aria-label="Thread notification settings" title="Notification Settings">
            <Icon name="bell" size={17} />
          </summary>
          <div class="notification-popover">
            <strong>Notifications</strong>
            {#each notificationOptions as option (option.value)}
              <button
                class:selected={notificationLevel === option.value}
                type="button"
                disabled={busy}
                onclick={() => void onNotifications(option.value)}
              >
                <span>{option.label}</span>
                {#if notificationLevel === option.value}<Icon name="check" size={15} />{/if}
              </button>
            {/each}
          </div>
        </details>
      {/if}
      <details>
        <summary
          ><Icon name="users" size={18} /><span>{thread.member_count ?? threadMembers.length}</span
          ></summary
        >
        <div class="member-popover">
          <strong>Thread Members</strong>
          {#if canInviteMembers || canRemoveMembers}<input
              bind:value={memberQuery}
              placeholder="Search members"
              aria-label="Search members"
            />{/if}
          <div>
            {#each canInviteMembers || canRemoveMembers ? visibleGuildMembers : guildMembers.filter( (member) => memberKeys.has(entityKey(member.user)) ) as member (entityKey(member.user))}
              <label>
                <span>{userDisplayName(member.user)}</span>
                {#if canInviteMembers || canRemoveMembers}
                  <input
                    type="checkbox"
                    checked={memberKeys.has(entityKey(member.user))}
                    disabled={busy ||
                      (memberKeys.has(entityKey(member.user))
                        ? !canRemoveMembers
                        : !canInviteMembers)}
                    onchange={(event) =>
                      void onMemberChange(entityKey(member.user), event.currentTarget.checked)}
                  />
                {/if}
              </label>
            {/each}
          </div>
        </div>
      </details>
      {#if forumPost && availableTags.length}
        <details>
          <summary>Tags</summary>
          <div class="tag-popover">
            {#each availableTags as tag (tag.id)}
              <label>
                <span>
                  <ForumTagEmoji
                    {tag}
                    guildId={parent?.guild_id ?? thread.guild_id}
                    guildDomain={parent?.guild_domain ?? thread.guild_domain}
                    {customEmojis}
                  />
                  {tag.name}
                </span>
                <input
                  type="checkbox"
                  checked={(thread.applied_tag_ids ?? []).includes(tag.id)}
                  disabled={busy ||
                    thread.archived ||
                    !canEdit ||
                    (tag.moderated && !canManage) ||
                    (!(thread.applied_tag_ids ?? []).includes(tag.id) &&
                      (thread.applied_tag_ids ?? []).length >= 5)}
                  onchange={() => toggleTag(tag.id)}
                />
              </label>
            {/each}
          </div>
        </details>
      {/if}
      {#if canEdit || canManage || canEnableEncryption || canRekeyEncryption}
        <details>
          <summary aria-label="Thread actions"><Icon name="more" size={19} /></summary>
          <div class="thread-menu">
            {#if canEnableEncryption || canRekeyEncryption}
              <button type="button" disabled={busy} onclick={() => void onEncryption()}>
                {canRekeyEncryption ? 'Secure Current Members' : 'Turn on End-to-End Encryption'}
              </button>
            {/if}
            {#if canEdit}
              <button type="button" disabled={busy || thread.archived} onclick={startRename}>
                Rename {forumPost ? 'Post' : 'Thread'}
              </button>
              <button
                type="button"
                disabled={busy}
                onclick={() => void onArchive(!thread.archived)}
              >
                {thread.archived
                  ? `Unarchive ${forumPost ? 'Post' : 'Thread'}`
                  : `Archive ${forumPost ? 'Post' : 'Thread'}`}
              </button>
              {#if thread.type === 12}
                <button
                  type="button"
                  disabled={busy || thread.archived}
                  onclick={() => void onInvitable(!thread.invitable)}
                >
                  {thread.invitable ? 'Disable Member Invites' : 'Allow Member Invites'}
                </button>
              {/if}
            {/if}
            {#if canManage}
              {#if forumPost}
                <button
                  type="button"
                  disabled={busy || thread.archived}
                  onclick={() => void onPin(!isPinnedForumPost(thread))}
                >
                  {isPinnedForumPost(thread) ? 'Unpin Post' : 'Pin Post'}
                </button>
              {/if}
              <button type="button" disabled={busy} onclick={() => void onLock(!thread.locked)}>
                {forumPost
                  ? thread.locked
                    ? 'Reopen Post'
                    : 'Close Post'
                  : thread.locked
                    ? 'Unlock Thread'
                    : 'Lock Thread'}
              </button>
              <button class="danger" type="button" disabled={busy} onclick={() => void onDelete()}>
                Delete {forumPost ? 'Post' : 'Thread'}
              </button>
            {/if}
          </div>
        </details>
      {/if}
    </div>
  </div>
  {#if renameOpen}
    <div class="rename-dialog-layer" role="presentation">
      <button
        class="rename-dialog-backdrop"
        type="button"
        aria-label="Close rename dialog"
        disabled={busy}
        onclick={() => (renameOpen = false)}
      ></button>
      <div
        class="rename-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-thread-title"
      >
        <header>
          <h2 id="rename-thread-title">Rename {forumPost ? 'Post' : 'Thread'}</h2>
          <button
            type="button"
            aria-label="Close"
            disabled={busy}
            onclick={() => (renameOpen = false)}>×</button
          >
        </header>
        <form
          onsubmit={(event) => {
            event.preventDefault();
            void submitRename();
          }}
        >
          <label>
            {forumPost ? 'Post' : 'Thread'} Name
            <input bind:value={renameName} maxlength="100" required disabled={busy} />
          </label>
          <footer>
            <button type="button" disabled={busy} onclick={() => (renameOpen = false)}>
              Cancel
            </button>
            <button
              class="primary"
              disabled={busy || !renameName.trim() || renameName.trim() === thread.name}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  {/if}
  {#if forumPost && starterMessage && reactionEmoji && !starterMessage.deleted_at && !starterMessage.content_unavailable}
    <div class="forum-post-actions">
      <button
        class="post-reaction-action"
        class:active={starterMessage.reacted_emoji?.includes(reactionEmoji)}
        type="button"
        disabled={busy || thread.archived || !canReact}
        onclick={reactToPost}
      >
        <ReactionEmoji value={reactionEmoji} />React to Post
      </button>
    </div>
  {/if}
  {#if encryptionStatus}
    <div class="thread-encryption" role="status">
      <Icon name="lock" size={16} />{encryptionStatus}
    </div>
  {/if}
</div>

<style>
  .thread-detail-header {
    display: grid;
    min-width: 0;
  }

  .rename-dialog-layer {
    position: fixed;
    z-index: 1200;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }

  .rename-dialog-backdrop {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: rgb(0 0 0 / 72%);
  }

  .rename-dialog {
    position: relative;
    width: min(440px, 100%);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1rem;
    background: var(--surface);
    box-shadow: var(--shadow-lg);
  }

  .rename-dialog header,
  .rename-dialog footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .rename-dialog h2 {
    margin: 0;
  }

  .rename-dialog header > button {
    border: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.4rem;
  }

  .rename-dialog form,
  .rename-dialog label {
    display: grid;
    gap: 0.55rem;
  }

  .rename-dialog form {
    margin-top: 1rem;
  }

  .rename-dialog label {
    color: var(--text-soft);
    font-size: 0.75rem;
    font-weight: 750;
  }

  .rename-dialog input {
    min-height: 42px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
  }

  .rename-dialog footer {
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .rename-dialog footer button {
    min-height: 38px;
    border: 0;
    border-radius: 7px;
    padding: 0 0.9rem;
    color: var(--text-soft);
    background: transparent;
    font-weight: 750;
  }

  .rename-dialog footer .primary {
    color: var(--on-accent);
    background: var(--accent);
  }

  .forum-post-actions {
    display: flex;
    min-height: 48px;
    align-items: center;
    border-bottom: 1px solid var(--line);
    padding: 0.4rem 0.8rem;
    background: var(--surface);
  }

  .forum-post-actions .post-reaction-action {
    margin-top: 0;
  }

  .thread-header {
    display: flex;
    min-width: 0;
    min-height: 56px;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line);
    padding: 0.45rem 0.8rem;
    background: var(--surface);
  }

  .thread-heading {
    min-width: 0;
  }

  .thread-breadcrumb,
  .thread-title,
  .thread-actions {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.4rem;
  }

  .thread-breadcrumb {
    color: var(--text-muted);
    font-size: 0.66rem;
  }

  .thread-breadcrumb a {
    color: inherit;
    text-decoration: none;
  }

  .thread-title strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .thread-title small {
    border-radius: 999px;
    padding: 0.15rem 0.35rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 0.6rem;
  }

  .thread-actions > button,
  summary {
    display: inline-flex;
    min-height: 34px;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0 0.55rem;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-size: 0.72rem;
    font-weight: 750;
    cursor: pointer;
    list-style: none;
  }

  summary::-webkit-details-marker {
    display: none;
  }

  details {
    position: relative;
  }

  .member-popover,
  .tag-popover,
  .notification-popover,
  .thread-menu {
    position: absolute;
    z-index: 20;
    top: calc(100% + 0.35rem);
    right: 0;
    display: grid;
    width: min(290px, calc(100vw - 2rem));
    gap: 0.55rem;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.7rem;
    background: var(--surface-raised);
    box-shadow: var(--shadow-md);
  }

  .member-popover > div {
    display: grid;
    max-height: 260px;
    overflow: auto;
  }

  .tag-popover {
    width: 230px;
  }

  .notification-popover {
    width: 210px;
  }

  .notification-popover button {
    display: flex;
    min-height: 34px;
    align-items: center;
    justify-content: space-between;
    border: 0;
    border-radius: 6px;
    padding: 0 0.5rem;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
  }

  .notification-popover button:hover,
  .notification-popover button.selected {
    background: var(--surface-hover);
  }

  .tag-popover label {
    display: flex;
    min-height: 34px;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    color: var(--text-soft);
    font-size: 0.75rem;
  }

  .member-popover > input {
    min-height: 36px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0 0.55rem;
    color: var(--text);
    background: var(--surface-subtle);
  }

  .member-popover label {
    display: flex;
    min-height: 34px;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    color: var(--text-soft);
    font-size: 0.75rem;
  }

  .thread-menu {
    width: 180px;
  }

  .thread-menu button {
    min-height: 34px;
    border: 0;
    border-radius: 6px;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
  }

  .thread-menu button.danger {
    color: var(--danger);
  }

  .thread-menu button:hover {
    background: var(--surface-hover);
  }

  .thread-encryption {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    border-bottom: 1px solid var(--line);
    padding: 0.45rem 0.8rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 0.72rem;
  }

  @media (max-width: 680px) {
    .thread-header {
      align-items: flex-start;
    }

    .thread-actions > button {
      font-size: 0;
    }

    .thread-actions > button :global(svg) {
      margin: auto;
    }

    .thread-breadcrumb {
      max-width: 46vw;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
</style>
