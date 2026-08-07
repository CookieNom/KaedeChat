<script lang="ts">
  import { assetUrl } from '$lib/media/assets';
  import { groupGuildMembers, memberDisplayName } from '$lib/chat/members';
  import type { GuildMemberSummary, PresenceStatus, Role, UserSummary } from '$lib/chat/types';
  import Icon from './Icon.svelte';

  let {
    members,
    roles = [],
    presenceFor,
    onProfile,
    onClose
  }: {
    members: GuildMemberSummary[];
    roles?: Role[];
    presenceFor: (user: UserSummary) => PresenceStatus;
    onProfile: (user: UserSummary, event: MouseEvent) => void;
    onClose: () => void;
  } = $props();

  const groups = $derived(groupGuildMembers(members, (member) => presenceFor(member.user), roles));
</script>

{#snippet memberRow(member: GuildMemberSummary, offline: boolean)}
  {@const presence = presenceFor(member.user)}
  {@const displayName = memberDisplayName(member)}
  <button
    class:offline
    class="roster-member"
    type="button"
    title={member.user.handle}
    oncontextmenu={(event) => onProfile(member.user, event)}
    onclick={(event) => onProfile(member.user, event)}
  >
    <span class="roster-avatar" aria-hidden="true">
      {#if member.user.avatar_hash}
        <img src={assetUrl(member.user.avatar_hash, 'thumbnail_128', member.user)} alt="" />
      {:else}
        {displayName.slice(0, 1).toUpperCase()}
      {/if}
      <i class={`presence-dot presence-${presence}`}></i>
    </span>
    <span class="roster-member-copy">
      <strong>{displayName}</strong>
      {#if member.user.custom_status?.trim()}
        <small title={member.user.custom_status}>{member.user.custom_status.trim()}</small>
      {/if}
    </span>
  </button>
{/snippet}

<aside class="member-roster" aria-label="Guild members">
  <header>
    <div>
      <Icon name="users" size={18} />
      <h2>Members</h2>
    </div>
    <button type="button" aria-label="Hide member list" title="Hide member list" onclick={onClose}
      >×</button
    >
  </header>
  <div class="member-roster-scroll">
    {#each groups.hoisted as group (group.role.id + '@' + group.role.origin_domain)}
      <section class="roster-group" aria-labelledby={`role-members-${group.role.id}`}>
        <h3
          id={`role-members-${group.role.id}`}
          style={`--role-color:#${group.role.color.toString(16).padStart(6, '0')}`}
        >
          {group.role.name} — {group.members.length}
        </h3>
        {#each group.members as member (member.user.id + '@' + member.user.origin_domain)}
          {@render memberRow(member, false)}
        {/each}
      </section>
    {/each}

    {#if groups.online.length}
      <section class="roster-group" aria-labelledby="online-members-heading">
        <h3 id="online-members-heading">Online — {groups.online.length}</h3>
        {#each groups.online as member (member.user.id + '@' + member.user.origin_domain)}
          {@render memberRow(member, false)}
        {/each}
      </section>
    {/if}

    {#if groups.offline.length}
      <section class="roster-group offline-group" aria-labelledby="offline-members-heading">
        <h3 id="offline-members-heading">Offline — {groups.offline.length}</h3>
        {#each groups.offline as member (member.user.id + '@' + member.user.origin_domain)}
          {@render memberRow(member, true)}
        {/each}
      </section>
    {/if}

    {#if !members.length}
      <p class="roster-empty">No members are available.</p>
    {/if}
  </div>
</aside>

<style>
  .member-roster {
    z-index: 2;
    display: flex;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    border-left: 1px solid var(--line-soft);
    background: var(--sidebar);
  }

  header {
    display: flex;
    min-height: 64px;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--line-soft);
    padding: 0 0.8rem 0 1rem;
  }

  header > div {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }

  h2,
  h3,
  p {
    margin: 0;
  }

  h2 {
    font-size: 0.86rem;
  }

  header button {
    display: grid;
    width: 34px;
    height: 34px;
    place-items: center;
    border: 0;
    border-radius: 9px;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.25rem;
    cursor: pointer;
  }

  header button:hover,
  header button:focus-visible {
    color: var(--text);
    background: var(--surface-hover);
  }

  .member-roster-scroll {
    min-height: 0;
    overflow-y: auto;
    padding: 0.7rem 0.55rem 1rem;
    scrollbar-color: var(--line) transparent;
    scrollbar-width: thin;
  }

  .roster-group + .roster-group {
    margin-top: 1rem;
  }

  h3 {
    padding: 0.35rem 0.55rem;
    color: var(--text-muted);
    font-size: 0.66rem;
    font-weight: 780;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h3[style] {
    color: var(--role-color);
  }

  .roster-member {
    display: flex;
    width: 100%;
    min-width: 0;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-radius: 8px;
    padding: 0.42rem 0.5rem;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .roster-member:hover,
  .roster-member:focus-visible {
    color: var(--text);
    background: var(--surface-hover);
  }

  .roster-member.offline {
    opacity: 0.5;
  }

  .roster-member.offline:hover,
  .roster-member.offline:focus-visible {
    opacity: 0.82;
  }

  .roster-avatar {
    position: relative;
    display: grid;
    width: 34px;
    height: 34px;
    flex: 0 0 auto;
    place-items: center;
    border-radius: 50%;
    color: var(--on-pine);
    background: var(--pine);
    font-size: 0.72rem;
    font-weight: 800;
  }

  .roster-avatar img {
    width: 100%;
    height: 100%;
    border-radius: inherit;
    clip-path: circle(50%);
    object-fit: cover;
  }

  .roster-avatar .presence-dot {
    position: absolute;
    right: -1px;
    bottom: -1px;
    border: 3px solid var(--sidebar);
  }

  .roster-member-copy {
    display: grid;
    min-width: 0;
    gap: 2px;
  }

  .roster-member-copy strong,
  .roster-member-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .roster-member-copy strong {
    font-size: 0.79rem;
    font-weight: 650;
  }

  .roster-member-copy small {
    color: var(--text-muted);
    font-size: 0.65rem;
  }

  .roster-empty {
    padding: 1rem 0.55rem;
    color: var(--text-muted);
    font-size: 0.74rem;
  }

  @media (max-width: 1000px) {
    .member-roster {
      position: fixed;
      z-index: 70;
      top: 0;
      right: 0;
      bottom: 0;
      width: min(268px, 86vw);
      box-shadow: var(--shadow-lg);
    }
  }
</style>
