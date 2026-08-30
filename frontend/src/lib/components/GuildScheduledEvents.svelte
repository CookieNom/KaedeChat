<script lang="ts">
  import { onDestroy, onMount, untrack } from 'svelte';
  import { userErrorMessage } from '$lib/api/client';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import {
    ScheduledEventEntityType,
    ScheduledEventRecurrencePreset,
    ScheduledEventStatus,
    createScheduledEvent,
    deleteScheduledEventImage,
    deleteScheduledEvent,
    editScheduledEvent,
    eventChannelRef,
    listScheduledEventUsers,
    scheduledEventRef,
    scheduledEventRecurrenceLabel,
    scheduledEventRecurrencePreset,
    scheduledEventStatusLabel,
    scheduledEventSubscriptionState,
    setScheduledEventSubscription,
    transitionScheduledEvent,
    uploadScheduledEventImage,
    type ScheduledEvent,
    type ScheduledEventDraft,
    type ScheduledEventUser
  } from '$lib/chat/scheduled-events';
  import type { Channel, UserSummary } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';
  import {
    canCreateScheduledEventInChannel,
    canManageScheduledEventInChannel
  } from '$lib/voice/stage-permissions';
  import { assetUrl } from '$lib/media/assets';
  import { formatDateTime } from '$lib/ui/locale';
  import Icon from './Icon.svelte';

  let {
    guildRef,
    channels,
    currentUser,
    canCreate,
    canCreateExternal,
    canManageExternal,
    events,
    startCreating = false,
    onEventsChange
  }: {
    guildRef: string;
    channels: Channel[];
    currentUser: UserSummary;
    canCreate: boolean;
    canCreateExternal: boolean;
    canManageExternal: boolean;
    events: ScheduledEvent[];
    startCreating?: boolean;
    onEventsChange: (events: ScheduledEvent[]) => void;
  } = $props();

  let editorOpen = $state(false);
  let editing = $state<ScheduledEvent | null>(null);
  let draft = $state<ScheduledEventDraft>(emptyDraft());
  let busyRef = $state('');
  let error = $state('');
  let notice = $state('');
  let expandedRef = $state('');
  let subscriberPages = $state<Record<string, ScheduledEventUser[]>>({});
  let subscriberLoading = $state<Record<string, boolean>>({});
  let subscribed = $state<Record<string, boolean | undefined>>({});
  let coverFile = $state<File | null>(null);
  let coverPreview = $state('');
  let removeCover = $state(false);
  let uploadProgress = $state(0);

  onDestroy(() => releaseCoverPreview());
  onMount(() => {
    if (startCreating && canCreate) openCreate();
  });

  $effect(() => {
    const current = untrack(() => subscribed);
    let next = current;
    for (const event of events) {
      if (typeof event.me_subscribed !== 'boolean') continue;
      const reference = scheduledEventRef(event);
      if (next[reference] === event.me_subscribed) continue;
      if (next === current) next = { ...current };
      next[reference] = event.me_subscribed;
    }
    if (next !== current) subscribed = next;
  });

  const eventChannels = $derived(
    channels
      .filter(
        (channel) =>
          channel.type === (draft.entityType === ScheduledEventEntityType.stage ? 13 : 2) &&
          canUseEventChannel(channel, editing)
      )
      .sort((left, right) => left.position - right.position)
  );

  function localDateTime(value: string): string {
    const date = new Date(value);
    if (!Number.isFinite(date.valueOf())) return '';
    const local = new Date(date.valueOf() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }

  function emptyDraft(): ScheduledEventDraft {
    const start = new Date(Math.ceil((Date.now() + 60 * 60 * 1000) / 60_000) * 60_000);
    return {
      name: '',
      description: '',
      entityType: ScheduledEventEntityType.voice,
      channelRef: '',
      location: '',
      startTime: localDateTime(start.toISOString()),
      endTime: '',
      recurrence: ScheduledEventRecurrencePreset.none
    };
  }

  function releaseCoverPreview() {
    if (coverPreview.startsWith('blob:')) URL.revokeObjectURL(coverPreview);
    coverPreview = '';
  }

  function resetCover(event: ScheduledEvent | null) {
    releaseCoverPreview();
    coverFile = null;
    removeCover = false;
    uploadProgress = 0;
    coverPreview = event?.image ? assetUrl(event.image, 'thumbnail_1024', event.origin_domain) : '';
  }

  function chooseCover(files: FileList | null) {
    const file = files?.item(0) ?? null;
    if (!file) return;
    if (!file.type.toLowerCase().startsWith('image/') || file.size > 10 * 1024 * 1024) {
      error = 'Choose an image up to 10 MiB.';
      return;
    }
    releaseCoverPreview();
    coverFile = file;
    coverPreview = URL.createObjectURL(file);
    removeCover = false;
    uploadProgress = 0;
    error = '';
  }

  function clearCover() {
    releaseCoverPreview();
    coverFile = null;
    removeCover = Boolean(editing?.image);
    uploadProgress = 0;
  }

  function canUseEventChannel(channel: Channel, event: ScheduledEvent | null): boolean {
    if (!event) return canCreateScheduledEventInChannel(channel);
    const own = `${event.creator_id}@${event.creator_domain}` === entityRef(currentUser);
    return canManageScheduledEventInChannel(channel, own);
  }

  function canManageEvent(event: ScheduledEvent): boolean {
    const own = `${event.creator_id}@${event.creator_domain}` === entityRef(currentUser);
    if (event.entity_type === ScheduledEventEntityType.external) {
      return canManageExternal || (canCreateExternal && own);
    }
    const reference = eventChannelRef(event);
    const channel = channels.find((item) => entityRef(item) === reference);
    const expectedType = event.entity_type === ScheduledEventEntityType.stage ? 13 : 2;
    return Boolean(channel?.type === expectedType && canUseEventChannel(channel, event));
  }

  function publish(next: ScheduledEvent[]) {
    const sorted = [...next].sort(
      (left, right) =>
        Date.parse(left.scheduled_start_time) - Date.parse(right.scheduled_start_time) ||
        scheduledEventRef(left).localeCompare(scheduledEventRef(right))
    );
    events = sorted;
    onEventsChange(sorted);
  }

  function replaceEvent(updated: ScheduledEvent) {
    publish(
      events.map((item) =>
        scheduledEventRef(item) === scheduledEventRef(updated) ? updated : item
      )
    );
  }

  function openCreate() {
    editing = null;
    draft = emptyDraft();
    const channel = channels.find((item) => canCreateScheduledEventInChannel(item));
    if (channel) {
      draft.entityType =
        channel.type === 13 ? ScheduledEventEntityType.stage : ScheduledEventEntityType.voice;
      draft.channelRef = entityRef(channel);
    } else if (canCreateExternal) {
      draft.entityType = ScheduledEventEntityType.external;
    }
    resetCover(null);
    editorOpen = true;
    error = '';
    notice = '';
  }

  function openEdit(event: ScheduledEvent) {
    editing = event;
    draft = {
      name: event.name,
      description: event.description ?? '',
      entityType: event.entity_type,
      channelRef: eventChannelRef(event) ?? '',
      location: event.entity_metadata?.location ?? '',
      startTime: localDateTime(event.scheduled_start_time),
      endTime: event.scheduled_end_time ? localDateTime(event.scheduled_end_time) : '',
      recurrence: scheduledEventRecurrencePreset(event.recurrence_rule)
    };
    resetCover(event);
    editorOpen = true;
    error = '';
    notice = '';
  }

  async function save() {
    if (busyRef) return;
    busyRef = editing ? scheduledEventRef(editing) : 'create';
    error = '';
    let detailsSaved = false;
    try {
      const previous = editing;
      let saved = previous
        ? await editScheduledEvent(guildRef, previous, draft)
        : await createScheduledEvent(guildRef, draft);
      if (previous) replaceEvent(saved);
      else publish([...events, saved]);
      detailsSaved = true;
      editing = saved;
      if (coverFile) {
        saved = await uploadScheduledEventImage(
          guildRef,
          saved,
          coverFile,
          (progress) => (uploadProgress = progress)
        );
        replaceEvent(saved);
      } else if (removeCover && saved.image) {
        saved = await deleteScheduledEventImage(guildRef, saved);
        replaceEvent(saved);
      }
      editorOpen = false;
      editing = null;
      resetCover(null);
      notice = `Event ${previous ? 'updated' : 'created'}.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        detailsSaved
          ? 'The event details were saved, but its cover could not be updated. Try the cover again.'
          : 'Could not save the scheduled event. Check its details and try again.'
      );
    } finally {
      busyRef = '';
    }
  }

  async function transition(event: ScheduledEvent, status: 2 | 3 | 4) {
    const reference = scheduledEventRef(event);
    if (busyRef) return;
    const label = status === 2 ? 'start' : status === 3 ? 'complete' : 'cancel';
    if (
      status === 4 &&
      !window.confirm(`Cancel “${event.name}”? Subscribers will no longer see it.`)
    ) {
      return;
    }
    busyRef = reference;
    error = '';
    try {
      const updated = await transitionScheduledEvent(guildRef, event, status);
      if (status === 3 || status === 4)
        publish(events.filter((item) => scheduledEventRef(item) !== reference));
      else replaceEvent(updated);
      notice =
        status === 2 ? 'Event started.' : status === 3 ? 'Event completed.' : 'Event canceled.';
    } catch (caught) {
      error = userErrorMessage(caught, `Could not ${label} this scheduled event. Try again.`);
    } finally {
      busyRef = '';
    }
  }

  async function remove(event: ScheduledEvent) {
    const reference = scheduledEventRef(event);
    if (busyRef || !window.confirm(`Delete “${event.name}” permanently?`)) return;
    busyRef = reference;
    error = '';
    try {
      await deleteScheduledEvent(guildRef, event);
      publish(events.filter((item) => scheduledEventRef(item) !== reference));
      notice = 'Scheduled event deleted.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not delete this scheduled event. Try again.');
    } finally {
      busyRef = '';
    }
  }

  async function showSubscribers(event: ScheduledEvent) {
    const reference = scheduledEventRef(event);
    if (expandedRef === reference) {
      expandedRef = '';
      return;
    }
    expandedRef = reference;
    if (subscriberPages[reference]) return;
    await loadSubscriberPage(event);
  }

  async function loadSubscriberPage(event: ScheduledEvent) {
    const reference = scheduledEventRef(event);
    if (subscriberLoading[reference]) return;
    subscriberLoading = { ...subscriberLoading, [reference]: true };
    error = '';
    try {
      const current = subscriberPages[reference] ?? [];
      const last = current.at(-1);
      const page = await listScheduledEventUsers(guildRef, event, {
        after: last ? entityRef(last.user) : undefined
      });
      const merged = [
        ...current,
        ...page.filter(
          (item) => !current.some((existing) => entityKey(existing.user) === entityKey(item.user))
        )
      ];
      subscriberPages = { ...subscriberPages, [reference]: merged };
      if (merged.some((item) => entityRef(item.user) === entityRef(currentUser))) {
        subscribed = { ...subscribed, [reference]: true };
      } else if (page.length < 100 || merged.length >= (event.user_count ?? 0)) {
        subscribed = { ...subscribed, [reference]: false };
      }
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load event subscribers. Try again.');
    } finally {
      subscriberLoading = { ...subscriberLoading, [reference]: false };
    }
  }

  async function toggleSubscription(event: ScheduledEvent) {
    const reference = scheduledEventRef(event);
    if (busyRef) return;
    const known = scheduledEventSubscriptionState(event, subscribed[reference]);
    const next = !known;
    busyRef = reference;
    error = '';
    try {
      await setScheduledEventSubscription(guildRef, event, next);
      subscribed = { ...subscribed, [reference]: next };
      const previousCount = event.user_count ?? subscriberPages[reference]?.length ?? 0;
      const delta = next ? 1 : -1;
      replaceEvent({
        ...event,
        user_count: Math.max(0, previousCount + delta),
        me_subscribed: next
      });
      if (next && subscriberPages[reference]) {
        subscriberPages = {
          ...subscriberPages,
          [reference]: [
            ...subscriberPages[reference].filter(
              (item) => entityRef(item.user) !== entityRef(currentUser)
            ),
            {
              guild_scheduled_event_id: event.id,
              guild_scheduled_event_domain: event.origin_domain,
              user: currentUser,
              member: null,
              subscribed_at: new Date().toISOString()
            }
          ]
        };
      } else if (!next && subscriberPages[reference]) {
        subscriberPages = {
          ...subscriberPages,
          [reference]: subscriberPages[reference].filter(
            (item) => entityRef(item.user) !== entityRef(currentUser)
          )
        };
      }
      notice = next ? 'You will be notified about this event.' : 'Event notifications turned off.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update event notifications. Try again.');
    } finally {
      busyRef = '';
    }
  }
</script>

<section id="scheduled-events" class="events-panel" aria-labelledby="scheduled-events-title">
  <header>
    <div>
      <h2 id="scheduled-events-title">Scheduled events</h2>
      <p>Plan voice gatherings or external events and let members follow updates.</p>
    </div>
    {#if canCreate}
      <button class="primary" type="button" onclick={openCreate} disabled={Boolean(busyRef)}>
        <Icon name="plus" size={16} /> Create event
      </button>
    {/if}
  </header>

  {#if error}<p class="banner error" role="alert">{error}</p>{/if}
  {#if notice}<p class="banner success" role="status">{notice}</p>{/if}

  <div class="event-list">
    {#each events as event (scheduledEventRef(event))}
      {@const reference = scheduledEventRef(event)}
      {@const following = scheduledEventSubscriptionState(event, subscribed[reference])}
      {@const channel = channels.find((item) => entityRef(item) === eventChannelRef(event))}
      {@const recurrence = scheduledEventRecurrenceLabel(event.recurrence_rule)}
      <article class:live={event.status === ScheduledEventStatus.active}>
        {#if event.image}
          <img
            class="event-cover"
            src={assetUrl(event.image, 'thumbnail_1024', event.origin_domain)}
            alt=""
          />
        {/if}
        <div class="date-tile" aria-hidden="true">
          <strong
            >{new Date(event.scheduled_start_time).toLocaleDateString(undefined, {
              month: 'short'
            })}</strong
          >
          <span>{new Date(event.scheduled_start_time).getDate()}</span>
        </div>
        <div class="event-copy">
          <div class="event-title">
            <h3>{event.name}</h3>
            <span>{scheduledEventStatusLabel(event.status)}</span>
          </div>
          <p class="event-time">{formatDateTime(event.scheduled_start_time)}</p>
          {#if recurrence}
            <p class="event-recurrence">{recurrence}</p>
          {/if}
          <p>
            {event.entity_type === ScheduledEventEntityType.stage
              ? `Stage · ${channel?.name ?? 'Unavailable channel'}`
              : event.entity_type === ScheduledEventEntityType.voice
                ? `Voice · ${channel?.name ?? 'Unavailable channel'}`
                : `External · ${event.entity_metadata?.location ?? 'Location unavailable'}`}
          </p>
          {#if event.description}<p class="description">{event.description}</p>{/if}
          <div class="actions">
            <button
              type="button"
              onclick={() => void toggleSubscription(event)}
              disabled={Boolean(busyRef)}
            >
              {following ? 'Following' : 'Notify me'}
            </button>
            <button
              type="button"
              onclick={() => void showSubscribers(event)}
              disabled={subscriberLoading[reference]}
            >
              {event.user_count ?? 0} interested
            </button>
            {#if canManageEvent(event)}
              <button type="button" onclick={() => openEdit(event)} disabled={Boolean(busyRef)}
                >Edit</button
              >
              {#if event.status === ScheduledEventStatus.scheduled}
                <button
                  type="button"
                  onclick={() => void transition(event, 2)}
                  disabled={Boolean(busyRef)}>Start</button
                >
                <button
                  class="danger-text"
                  type="button"
                  onclick={() => void transition(event, 4)}
                  disabled={Boolean(busyRef)}>Cancel</button
                >
              {:else if event.status === ScheduledEventStatus.active}
                <button
                  type="button"
                  onclick={() => void transition(event, 3)}
                  disabled={Boolean(busyRef)}>Complete</button
                >
              {/if}
              <button
                class="danger-text"
                type="button"
                onclick={() => void remove(event)}
                disabled={Boolean(busyRef)}>Delete</button
              >
            {/if}
          </div>
          {#if expandedRef === reference}
            <div class="subscribers">
              <strong>Interested members</strong>
              {#if subscriberLoading[reference] && !subscriberPages[reference]?.length}
                <p>Loading members…</p>
              {:else}
                <ul>
                  {#each subscriberPages[reference] ?? [] as subscription (entityKey(subscription.user))}
                    <li>{subscription.member?.nickname ?? userDisplayName(subscription.user)}</li>
                  {:else}
                    <li>No one has followed this event yet.</li>
                  {/each}
                </ul>
                {#if (subscriberPages[reference]?.length ?? 0) < (event.user_count ?? 0)}
                  <button
                    type="button"
                    onclick={() => void loadSubscriberPage(event)}
                    disabled={subscriberLoading[reference]}
                  >
                    {subscriberLoading[reference] ? 'Loading…' : 'Load more'}
                  </button>
                {/if}
              {/if}
            </div>
          {/if}
        </div>
      </article>
    {:else}
      <div class="empty">
        <span aria-hidden="true">◷</span>
        <strong>No upcoming events</strong>
        <p>
          {canCreate
            ? 'Create one when your community has something planned.'
            : 'Nothing is scheduled yet.'}
        </p>
      </div>
    {/each}
  </div>
</section>

{#if editorOpen}
  <div class="modal-backdrop" role="presentation">
    <div class="event-modal" role="dialog" aria-modal="true" aria-labelledby="event-editor-title">
      <form
        class="event-modal-form"
        onsubmit={(submitEvent) => {
          submitEvent.preventDefault();
          void save();
        }}
      >
        <header>
          <div>
            <h2 id="event-editor-title">{editing ? 'Edit event' : 'Create event'}</h2>
            <p>Choose a Stage, voice channel, or external location.</p>
          </div>
          <button
            class="close"
            type="button"
            aria-label="Close"
            onclick={() => (editorOpen = false)}>×</button
          >
        </header>
        <label>
          <span>Name</span>
          <input bind:value={draft.name} maxlength="100" required disabled={Boolean(busyRef)} />
        </label>
        <label>
          <span>Description <small>optional</small></span>
          <textarea
            bind:value={draft.description}
            maxlength="1000"
            rows="3"
            disabled={Boolean(busyRef)}
          ></textarea>
        </label>
        <label>
          <span>Cover image <small>optional · up to 10 MiB</small></span>
          {#if coverPreview}
            <img class="cover-preview" src={coverPreview} alt="Event cover preview" />
          {/if}
          <div class="cover-controls">
            <input
              type="file"
              accept="image/*"
              disabled={Boolean(busyRef)}
              onchange={(changeEvent) =>
                chooseCover((changeEvent.currentTarget as HTMLInputElement).files)}
            />
            {#if coverPreview}
              <button type="button" onclick={clearCover} disabled={Boolean(busyRef)}>Remove</button>
            {/if}
          </div>
          {#if uploadProgress > 0 && uploadProgress < 100}
            <small>Uploading cover… {uploadProgress}%</small>
          {/if}
        </label>
        <div class="form-grid">
          <label>
            <span>Event type</span>
            <select
              bind:value={draft.entityType}
              disabled={Boolean(busyRef) || editing?.status === ScheduledEventStatus.active}
            >
              <option value={ScheduledEventEntityType.stage}>Stage channel</option>
              <option value={ScheduledEventEntityType.voice}>Voice channel</option>
              {#if canCreateExternal || editing?.entity_type === ScheduledEventEntityType.external}
                <option value={ScheduledEventEntityType.external}>External</option>
              {/if}
            </select>
          </label>
          <label>
            <span>Repeat</span>
            <select
              bind:value={draft.recurrence}
              disabled={Boolean(busyRef) || editing?.status === ScheduledEventStatus.active}
            >
              <option value={ScheduledEventRecurrencePreset.none}>Does not repeat</option>
              <option value={ScheduledEventRecurrencePreset.daily}>Daily</option>
              <option value={ScheduledEventRecurrencePreset.weekly}>Weekly</option>
              <option value={ScheduledEventRecurrencePreset.biweekly}>Every 2 weeks</option>
              <option value={ScheduledEventRecurrencePreset.monthly}>Monthly</option>
              <option value={ScheduledEventRecurrencePreset.yearly}>Yearly</option>
            </select>
          </label>
          {#if draft.entityType !== ScheduledEventEntityType.external}
            <label>
              <span
                >{draft.entityType === ScheduledEventEntityType.stage
                  ? 'Stage channel'
                  : 'Voice channel'}</span
              >
              <select bind:value={draft.channelRef} required disabled={Boolean(busyRef)}>
                <option value="">Choose a channel</option>
                {#each eventChannels as channel (entityKey(channel))}
                  <option value={entityRef(channel)}>{channel.name}</option>
                {/each}
              </select>
              {#if !eventChannels.length}<small
                  >You need Create Events, View Channel, and Connect in a matching channel.</small
                >{/if}
            </label>
          {:else}
            <label>
              <span>Location or link</span>
              <input
                bind:value={draft.location}
                maxlength="100"
                required
                disabled={Boolean(busyRef)}
              />
            </label>
          {/if}
          <label>
            <span>Starts</span>
            <input
              type="datetime-local"
              bind:value={draft.startTime}
              required
              disabled={Boolean(busyRef) || editing?.status === ScheduledEventStatus.active}
            />
          </label>
          <label>
            <span
              >Ends {draft.entityType !== ScheduledEventEntityType.external
                ? '· optional'
                : ''}</span
            >
            <input
              type="datetime-local"
              bind:value={draft.endTime}
              required={draft.entityType === ScheduledEventEntityType.external}
              disabled={Boolean(busyRef)}
            />
          </label>
        </div>
        {#if error}<p class="banner error" role="alert">{error}</p>{/if}
        <footer>
          <button type="button" onclick={() => (editorOpen = false)} disabled={Boolean(busyRef)}
            >Cancel</button
          >
          <button class="primary" disabled={Boolean(busyRef)}
            >{busyRef ? 'Saving…' : 'Save event'}</button
          >
        </footer>
      </form>
    </div>
  </div>
{/if}

<style>
  .events-panel {
    display: grid;
    gap: 16px;
    scroll-margin-top: 20px;
  }
  .events-panel > header,
  .event-modal-form > header,
  .event-modal-form footer,
  .event-title,
  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  h2,
  h3,
  p {
    margin: 0;
  }
  header p,
  article p,
  .empty p,
  .subscribers {
    color: var(--text-muted, #9c938a);
  }
  button,
  input,
  select,
  textarea {
    font: inherit;
  }
  button {
    border: 1px solid var(--border, #403a35);
    border-radius: 8px;
    padding: 8px 11px;
    color: inherit;
    background: var(--surface-raised, #2a2724);
    cursor: pointer;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .primary {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border-color: transparent;
    color: white;
    background: var(--accent, #cc6d4b);
  }
  .event-list {
    display: grid;
    gap: 10px;
  }
  article {
    position: relative;
    display: grid;
    grid-template-columns: 54px minmax(0, 1fr);
    gap: 14px;
    border: 1px solid var(--border, #403a35);
    border-radius: 12px;
    padding: 15px;
    background: var(--surface-raised, #25221f);
  }
  .event-cover {
    grid-column: 1 / -1;
    width: 100%;
    max-height: 240px;
    border-radius: 9px;
    object-fit: cover;
  }
  article.live {
    border-color: var(--accent, #cc6d4b);
  }
  .date-tile {
    display: grid;
    place-content: center;
    text-align: center;
    height: 54px;
    border-radius: 10px;
    background: color-mix(in srgb, var(--accent, #cc6d4b) 18%, transparent);
  }
  .date-tile strong {
    color: var(--accent, #e9825e);
    font-size: 11px;
    text-transform: uppercase;
  }
  .date-tile span {
    font-size: 20px;
    font-weight: 700;
  }
  .event-copy {
    min-width: 0;
    display: grid;
    gap: 5px;
  }
  .event-title {
    justify-content: flex-start;
  }
  .event-title > span {
    border-radius: 999px;
    padding: 2px 7px;
    color: var(--accent, #e9825e);
    background: color-mix(in srgb, var(--accent, #cc6d4b) 14%, transparent);
    font-size: 11px;
    font-weight: 700;
  }
  .event-time {
    color: var(--text, #efe8e1);
    font-weight: 600;
  }
  .event-recurrence {
    font-size: 13px;
  }
  .description {
    white-space: pre-wrap;
  }
  .actions {
    justify-content: flex-start;
    flex-wrap: wrap;
    margin-top: 8px;
  }
  .danger-text {
    color: var(--danger, #f08080);
  }
  .subscribers {
    border-top: 1px solid var(--border, #403a35);
    margin-top: 8px;
    padding-top: 10px;
  }
  .subscribers ul {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    margin: 8px 0;
    padding-left: 18px;
  }
  .banner {
    border-radius: 8px;
    padding: 10px 12px;
  }
  .banner.error {
    color: var(--danger, #f4a3a3);
    background: rgb(128 32 32 / 24%);
  }
  .banner.success {
    color: #92d8ad;
    background: rgb(26 108 60 / 22%);
  }
  .empty {
    display: grid;
    place-items: center;
    gap: 5px;
    border: 1px dashed var(--border, #403a35);
    border-radius: 12px;
    padding: 30px;
    text-align: center;
  }
  .empty > span {
    font-size: 30px;
  }
  .modal-backdrop {
    position: fixed;
    z-index: 1000;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 20px;
    background: rgb(0 0 0 / 62%);
  }
  .event-modal {
    width: min(660px, 100%);
    max-height: 90vh;
    overflow: auto;
    display: grid;
    gap: 16px;
    border: 1px solid var(--border, #403a35);
    border-radius: 14px;
    padding: 20px;
    color: var(--text, #efe8e1);
    background: var(--surface, #1f1c1a);
    box-shadow: 0 24px 80px rgb(0 0 0 / 45%);
  }
  .event-modal-form {
    display: contents;
  }
  .close {
    border: 0;
    padding: 4px 8px;
    font-size: 24px;
    background: transparent;
  }
  label {
    display: grid;
    gap: 6px;
  }
  label > span {
    font-size: 13px;
    font-weight: 650;
  }
  label small {
    color: var(--text-muted, #9c938a);
  }
  .cover-preview {
    width: 100%;
    max-height: 230px;
    border-radius: 9px;
    object-fit: cover;
  }
  .cover-controls {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  input,
  select,
  textarea {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border, #403a35);
    border-radius: 8px;
    padding: 10px;
    color: inherit;
    background: var(--input, #191715);
  }
  textarea {
    resize: vertical;
  }
  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  .event-modal footer {
    justify-content: flex-end;
  }
  @media (max-width: 620px) {
    .events-panel > header,
    .event-modal-form > header {
      align-items: flex-start;
    }
    .form-grid {
      grid-template-columns: 1fr;
    }
    article {
      grid-template-columns: 45px minmax(0, 1fr);
      padding: 12px;
    }
  }
</style>
