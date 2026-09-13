<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { userErrorMessage } from '$lib/api/client';
  import {
    announcementTargets,
    canDeleteAnnouncementFollow,
    createAnnouncementFollow,
    deleteAnnouncementFollow,
    listAnnouncementFollows,
    type AnnouncementFollow
  } from '$lib/chat/announcements';
  import { entityRef } from '$lib/chat/refs';
  import type { Channel, Guild } from '$lib/chat/types';
  import Icon from './Icon.svelte';

  let {
    sourceChannel,
    guilds,
    canRead,
    mode = 'manage',
    manageTitle = 'Channels Followed',
    manageDescription = 'Manage text channels that receive published posts from this announcement channel, including destinations in federated guilds.'
  }: {
    sourceChannel: Channel;
    guilds: Guild[];
    canRead: boolean;
    mode?: 'create' | 'manage';
    manageTitle?: string;
    manageDescription?: string;
  } = $props();

  let follows = $state<AnnouncementFollow[]>([]);
  let loading = $state(false);
  let busyFollowId = $state<string | null>(null);
  let selectedTarget = $state('');
  let error = $state('');
  let notice = $state('');
  let requestGeneration = 0;

  const targets = $derived(announcementTargets(guilds));
  const followedTargetRefs = $derived(
    new Set(follows.map((follow) => `${follow.target_channel_id}@${follow.target_channel_domain}`))
  );
  const availableTargets = $derived(
    targets.filter((target) => !followedTargetRefs.has(target.ref))
  );
  const targetByRef = $derived(new Map(targets.map((target) => [target.ref, target])));
  const knownTargetLabels = $derived(
    new Map(
      guilds.flatMap((guild) =>
        (guild.channels ?? []).map(
          (channel) =>
            [entityRef(channel), `${guild.name} · #${channel.name ?? 'channel'}`] as const
        )
      )
    )
  );

  function targetLabel(follow: AnnouncementFollow): string {
    const ref = `${follow.target_channel_id}@${follow.target_channel_domain}`;
    return targetByRef.get(ref)?.label ?? knownTargetLabels.get(ref) ?? `Channel ${ref}`;
  }

  async function load(sourceRef: string, generation: number, signal: AbortSignal) {
    loading = true;
    error = '';
    follows = [];
    try {
      const loaded = await listAnnouncementFollows(sourceRef, signal);
      if (generation !== requestGeneration) return;
      follows = loaded.filter((follow) => follow.active);
    } catch (caught) {
      if (signal.aborted || generation !== requestGeneration) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_load_announcement_followers_check_y_7eecb641')
      );
    } finally {
      if (generation === requestGeneration) loading = false;
    }
  }

  $effect(() => {
    const sourceRef = entityRef(sourceChannel);
    const readable = canRead;
    const generation = ++requestGeneration;
    error = '';
    notice = '';
    selectedTarget = '';
    follows = [];
    if (!readable) {
      loading = false;
      return;
    }
    const controller = new AbortController();
    void load(sourceRef, generation, controller.signal);
    return () => controller.abort();
  });

  async function createFollow() {
    if (!canRead || !selectedTarget || busyFollowId) return;
    const sourceRef = entityRef(sourceChannel);
    const targetRef = selectedTarget;
    busyFollowId = 'create';
    error = '';
    notice = '';
    try {
      const created = await createAnnouncementFollow(sourceRef, targetRef);
      follows = [...follows.filter((follow) => follow.ref !== created.ref), created];
      selectedTarget = '';
      notice = $t('ui_new_announcement_posts_can_now_be_published_t_31eedf98');
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_follow_this_announcement_channel_yo_0ae2a661')
      );
    } finally {
      busyFollowId = null;
    }
  }

  async function removeFollow(follow: AnnouncementFollow) {
    if (busyFollowId || !canDeleteAnnouncementFollow(follow, guilds)) return;
    const label = targetLabel(follow);
    if (!window.confirm(`Stop publishing new announcements to ${label}?`)) return;
    busyFollowId = follow.ref;
    error = '';
    notice = '';
    try {
      await deleteAnnouncementFollow(entityRef(sourceChannel), follow.ref);
      follows = follows.filter((item) => item.ref !== follow.ref);
      notice = `Stopped publishing announcements to ${label}.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_remove_this_follower_you_need_manag_ed645a81')
      );
    } finally {
      busyFollowId = null;
    }
  }
</script>

<section
  class="announcement-followers"
  class:create-mode={mode === 'create'}
  aria-labelledby="announcement-followers-title"
>
  <div class="announcement-heading">
    <span class="announcement-icon" aria-hidden="true"><Icon name="bell" size={18} /></span>
    <div>
      <span
        >{mode === 'create'
          ? $t('ui_follow_announcement_channel_d4bae827')
          : $t('ui_announcement_distribution_21b34f29')}</span
      >
      <h4 id="announcement-followers-title">
        {mode === 'create' ? `Follow #${sourceChannel.name ?? 'announcements'}` : manageTitle}
      </h4>
      <p>
        {mode === 'create'
          ? $t('ui_choose_a_text_channel_where_you_can_manage_we_464e5673')
          : manageDescription}
      </p>
    </div>
  </div>

  {#if !canRead}
    <div class="announcement-state locked" role="note">
      <Icon name="lock" size={18} />
      <span>{$t('ui_you_need_view_channel_and_read_message_histor_b0b502f3')}</span>
    </div>
  {:else}
    <form
      class="follow-form"
      onsubmit={(event) => {
        event.preventDefault();
        void createFollow();
      }}
    >
      <label>
        <span>{$t('ui_publish_into_e49d1bfc')}</span>
        <select
          bind:value={selectedTarget}
          disabled={Boolean(busyFollowId) || loading || availableTargets.length === 0}
          aria-describedby="announcement-target-help"
        >
          <option value="">{$t('ui_choose_a_destination_ee63978f')}</option>
          {#each availableTargets as target (target.ref)}
            <option value={target.ref}>{target.label}</option>
          {/each}
        </select>
      </label>
      <button class="primary-button" disabled={Boolean(busyFollowId) || loading || !selectedTarget}>
        {busyFollowId === 'create' ? $t('ui_following_408ac0c4') : $t('ui_follow_641d1ef6')}
      </button>
    </form>
    <p id="announcement-target-help" class="help-copy">
      {#if targets.length === 0}{$t(
          'ui_no_eligible_destinations_are_available_you_ne_2d6e1433'
        )}{:else if availableTargets.length === 0}{$t(
          'ui_every_eligible_destination_already_follows_th_d361a7ba'
        )}{:else}{$t('ui_publishing_is_deliberate_ordinary_messages_ar_3d71d30a')}{/if}
    </p>

    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    {#if notice}<p class="form-success" role="status">{notice}</p>{/if}

    {#if mode === 'manage' && loading}
      <div class="announcement-state" role="status">
        {$t('ui_loading_follower_channels_0c54ad77')}
      </div>
    {:else if mode === 'manage' && follows.length === 0}
      <div class="announcement-state">
        {$t('ui_no_channels_follow_this_announcement_channel__ca9af932')}
      </div>
    {:else if mode === 'manage'}
      <div class="follow-list" aria-label={$t('ui_announcement_follower_channels_86cf0004')}>
        {#each follows as follow (follow.ref)}
          {@const targetRef = `${follow.target_channel_id}@${follow.target_channel_domain}`}
          {@const canDelete = canDeleteAnnouncementFollow(follow, guilds)}
          <div class="follow-row">
            <span class="follow-mark" aria-hidden="true">#</span>
            <span class="follow-details">
              <strong>{targetLabel(follow)}</strong>
              <small>
                {targetRef}{follow.federated ? $t('ui_federated_bfc21067') : ''}
              </small>
            </span>
            <button
              class="danger-text-button"
              type="button"
              disabled={Boolean(busyFollowId) || !canDelete}
              title={canDelete
                ? `Stop publishing to ${targetLabel(follow)}`
                : $t('ui_manage_webhooks_is_required_in_the_destinatio_fc5e198d')}
              onclick={() => void removeFollow(follow)}
            >
              {busyFollowId === follow.ref ? $t('ui_removing_d4b09919') : $t('ui_remove_c3812fc4')}
            </button>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</section>

<style>
  .announcement-followers {
    display: grid;
    gap: 14px;
    padding-top: 22px;
    margin-top: 22px;
    border-top: 1px solid var(--border, rgba(255, 255, 255, 0.1));
  }

  .announcement-followers.create-mode {
    margin-top: 0;
    padding-top: 0;
    border-top: 0;
  }

  .announcement-heading {
    display: flex;
    align-items: flex-start;
    gap: 11px;
  }

  .announcement-icon,
  .follow-mark {
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    width: 34px;
    height: 34px;
    border-radius: 10px;
    color: var(--accent, #8d86ff);
    background: color-mix(in srgb, var(--accent, #8d86ff) 14%, transparent);
  }

  .announcement-heading span,
  .follow-form label > span {
    color: var(--text-muted, #aaa7b4);
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .announcement-heading h4 {
    margin: 2px 0 4px;
  }

  .announcement-heading p,
  .help-copy {
    margin: 0;
    color: var(--text-muted, #aaa7b4);
    line-height: 1.45;
  }

  .follow-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
    gap: 10px;
  }

  .follow-form label {
    display: grid;
    gap: 6px;
  }

  .follow-form select {
    width: 100%;
    min-height: 42px;
  }

  .help-copy {
    font-size: 0.82rem;
  }

  .announcement-state {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px;
    border: 1px solid var(--border, rgba(255, 255, 255, 0.1));
    border-radius: 10px;
    color: var(--text-muted, #aaa7b4);
  }

  .announcement-state.locked {
    color: var(--warning, #f5c46b);
  }

  .follow-list {
    display: grid;
    gap: 8px;
  }

  .follow-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    border: 1px solid var(--border, rgba(255, 255, 255, 0.1));
    border-radius: 10px;
  }

  .follow-mark {
    width: 30px;
    height: 30px;
    font-weight: 800;
  }

  .follow-details {
    display: grid;
    min-width: 0;
    margin-right: auto;
  }

  .follow-details strong,
  .follow-details small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .follow-details small {
    color: var(--text-muted, #aaa7b4);
  }

  @media (max-width: 620px) {
    .follow-form {
      grid-template-columns: 1fr;
    }
  }
</style>
