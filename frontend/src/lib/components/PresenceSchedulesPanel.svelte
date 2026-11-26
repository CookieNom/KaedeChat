<script lang="ts">
  import { onMount } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import { ApiError } from '$lib/api/errors';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import {
    COMMON_TIMEZONES,
    DAY_NAMES,
    DAY_SHORT_LABELS,
    MAX_PRESENCE_SCHEDULES,
    detectDeviceTimezone,
    formatDaysSummary,
    formatMinuteOfDay,
    formatTimestampHint,
    minuteToTimeInput,
    presenceControl,
    presenceStatusLabel,
    scheduleMatchesAt,
    timeInputToMinute,
    type PresencePreference,
    type PresenceScheduleRule,
    type PresenceStatePayload
  } from '$lib/stores/presence.svelte';
  import Icon from './Icon.svelte';

  let {
    compact = false,
    onSaved
  }: {
    compact?: boolean;
    onSaved?: (state: PresenceStatePayload) => void;
  } = $props();

  const deviceTimezone = detectDeviceTimezone();
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let selectedDay = $state<number | null>(null);
  let now = $state(Date.now());
  onMount(() => {
    const timer = setInterval(() => {
      now = Date.now();
    }, 30_000);
    return () => clearInterval(timer);
  });

  // Inline editor state
  let editing = $state(false);
  let editingId = $state<string | null>(null);
  let editorSchedules: PresenceScheduleRule[] = [];
  let draftName = $state('');
  let draftStatus = $state<PresencePreference>('dnd');
  let draftDays = $state<number[]>([0, 1, 2, 3, 4, 5, 6]);
  let draftStart = $state('23:00');
  let draftEnd = $state('07:00');

  const currentState = $derived(presenceControl.state);
  const schedules = $derived(currentState?.schedules ?? []);
  const savedTimezone = $derived(currentState?.timezone || deviceTimezone);
  const timezoneChoices = $derived.by(() => {
    const zones = [...COMMON_TIMEZONES];
    if (deviceTimezone && !zones.includes(deviceTimezone)) zones.push(deviceTimezone);
    if (savedTimezone && !zones.includes(savedTimezone)) zones.push(savedTimezone);
    return zones;
  });
  const timezoneDiffersFromDevice = $derived(
    !!currentState && deviceTimezone !== '' && savedTimezone !== deviceTimezone
  );

  const draftStartMinute = $derived(timeInputToMinute(draftStart));
  const draftEndMinute = $derived(timeInputToMinute(draftEnd));
  const draftIsOvernight = $derived(draftEndMinute < draftStartMinute);
  const draftSameTime = $derived(draftStartMinute === draftEndMinute);
  const draftValid = $derived(
    draftName.trim().length > 0 && draftDays.length > 0 && !draftSameTime
  );

  const zoneClock = $derived.by(() => {
    const options: Intl.DateTimeFormatOptions = {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23'
    };
    try {
      return new Intl.DateTimeFormat('en-US', { ...options, timeZone: savedTimezone || 'UTC' });
    } catch {
      // Older browsers may lack a recently added IANA zone. Hide its marker.
      return null;
    }
  });
  const clock = $derived(zoneClock?.formatToParts(now) ?? []);
  const todayIndex = $derived(
    DAY_NAMES.findIndex((day) => day === clock.find((part) => part.type === 'weekday')?.value)
  );
  const previewDay = $derived(selectedDay ?? Math.max(0, todayIndex));
  const nowPercent = $derived(
    ((Number(clock.find((part) => part.type === 'hour')?.value) * 60 +
      Number(clock.find((part) => part.type === 'minute')?.value)) *
      100) /
      1440
  );

  interface TimelineSlice {
    status: PresencePreference;
    label: string;
    overlapped: boolean;
  }

  const timelineSlices = $derived.by((): { slices: TimelineSlice[]; hasOverlap: boolean } => {
    const slices: TimelineSlice[] = [];
    let hasOverlap = false;
    const base = currentState?.base_status ?? 'online';
    for (let slot = 0; slot < 96; slot += 1) {
      const minute = slot * 15;
      const matches = schedules.filter((rule) => scheduleMatchesAt(rule, previewDay, minute));
      if (matches.length > 1) hasOverlap = true;
      const winner = matches[0];
      const status: PresencePreference =
        base === 'invisible' ? 'invisible' : (winner?.status ?? base);
      const timeLabel = formatMinuteOfDay(minute);
      const label = winner
        ? `${timeLabel}: ${winner.name} (${presenceStatusLabel(winner.status)})`
        : `${timeLabel}: ${presenceStatusLabel(base)}`;
      slices.push({ status, label, overlapped: matches.length > 1 });
    }
    return { slices, hasOverlap };
  });

  const presets: ReadonlyArray<{
    id: string;
    label: string;
    hint: string;
    name: string;
    status: PresencePreference;
    days: number[];
    start_minute: number;
    end_minute: number;
  }> = [
    {
      id: 'sleep',
      label: 'Sleep',
      hint: '11 PM–7 AM · DND',
      name: 'Sleep',
      status: 'dnd',
      days: [0, 1, 2, 3, 4, 5, 6],
      start_minute: 23 * 60,
      end_minute: 7 * 60
    },
    {
      id: 'work',
      label: 'Work Focus',
      hint: 'Mon–Fri 9 AM–5 PM · Idle',
      name: 'Work Focus',
      status: 'idle',
      days: [1, 2, 3, 4, 5],
      start_minute: 9 * 60,
      end_minute: 17 * 60
    },
    {
      id: 'weekend',
      label: 'Weekend Quiet',
      hint: 'Sat–Sun 10 PM–9 AM · DND',
      name: 'Weekend Quiet',
      status: 'dnd',
      days: [0, 6],
      start_minute: 22 * 60,
      end_minute: 9 * 60
    }
  ];

  const statusChoices: ReadonlyArray<{ value: PresencePreference; label: string }> = [
    { value: 'online', label: 'Online' },
    { value: 'idle', label: 'Idle' },
    { value: 'dnd', label: 'Do Not Disturb' },
    { value: 'invisible', label: 'Invisible' }
  ];

  async function persistPatch(
    body: Record<string, unknown>,
    successMessage: string,
    expectedSchedules = schedules
  ) {
    if (busy) return;
    busy = true;
    error = '';
    notice = '';
    try {
      const updated = await api<{
        presence_preference: PresencePreference;
        presence_state?: PresenceStatePayload;
      }>('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify(
          'presence_schedules' in body
            ? { ...body, expected_presence_schedules: expectedSchedules }
            : body
        )
      });
      if (updated.presence_state) {
        presenceControl.apply(updated.presence_state);
        onSaved?.(updated.presence_state);
      } else {
        presenceControl.applyFallbackPreference(updated.presence_preference);
      }
      authenticatedGateway.client.rememberPresence?.(updated.presence_preference);
      if (chatEntities.currentUser) {
        chatEntities.setPresence(
          chatEntities.currentUser,
          updated.presence_preference === 'invisible' ? 'offline' : updated.presence_preference
        );
      }
      notice = successMessage;
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'PRESENCE_SCHEDULES_CHANGED') {
        editing = false;
        try {
          const latest = await api<{ presence_state: PresenceStatePayload }>('/users/@me/settings');
          presenceControl.apply(latest.presence_state);
          error =
            'Your schedules changed on another device. Review the updated list and try again.';
        } catch {
          error = 'Your schedules changed on another device. Reload settings before trying again.';
        }
      } else {
        error = userErrorMessage(caught, 'Could not save your presence schedule.');
      }
    } finally {
      busy = false;
    }
  }

  function openNewEditor(preset?: (typeof presets)[number]) {
    if (schedules.length >= MAX_PRESENCE_SCHEDULES) return;
    editorSchedules = $state.snapshot(schedules);
    editingId = null;
    draftName = preset ? preset.name : 'Quiet Hours';
    draftStatus = preset ? preset.status : 'dnd';
    draftDays = preset ? [...preset.days] : [0, 1, 2, 3, 4, 5, 6];
    draftStart = minuteToTimeInput(preset ? preset.start_minute : 22 * 60);
    draftEnd = minuteToTimeInput(preset ? preset.end_minute : 7 * 60);
    editing = true;
    error = '';
    notice = '';
  }

  function openEditEditor(rule: PresenceScheduleRule) {
    editorSchedules = $state.snapshot(schedules);
    editingId = rule.id;
    draftName = rule.name;
    draftStatus = rule.status;
    draftDays = [...rule.days];
    draftStart = minuteToTimeInput(rule.start_minute);
    draftEnd = minuteToTimeInput(rule.end_minute);
    editing = true;
    error = '';
    notice = '';
  }

  function toggleDraftDay(day: number) {
    if (draftDays.includes(day)) {
      draftDays = draftDays.filter((item) => item !== day);
    } else {
      draftDays = [...draftDays, day].sort((a, b) => a - b);
    }
  }

  function nextScheduleId(): string {
    const existingIds = new Set(editorSchedules.map((rule) => rule.id));
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const candidate = `sched_${Date.now().toString(36)}_${attempt}`;
      if (!existingIds.has(candidate)) return candidate;
    }
    return `sched_${Math.random().toString(36).slice(2, 10)}`;
  }

  async function saveEditor(event: SubmitEvent) {
    event.preventDefault();
    if (!draftValid || busy) return;
    const cleanedName = draftName.trim().slice(0, 32);
    const nextRule: PresenceScheduleRule = {
      id: editingId ?? nextScheduleId(),
      name: cleanedName,
      status: draftStatus,
      days: [...draftDays].sort((a, b) => a - b),
      start_minute: draftStartMinute,
      end_minute: draftEndMinute,
      enabled: editorSchedules.find((rule) => rule.id === editingId)?.enabled ?? true
    };
    const nextSchedules = editingId
      ? editorSchedules.map((rule) => (rule.id === editingId ? nextRule : rule))
      : [...editorSchedules, nextRule];
    const body: Record<string, unknown> = { presence_schedules: nextSchedules };
    if (!currentState?.timezone && deviceTimezone) {
      body.presence_timezone = deviceTimezone;
    }
    await persistPatch(body, editingId ? 'Schedule updated.' : 'Schedule added.', editorSchedules);
    if (!error) {
      editing = false;
      editingId = null;
    }
  }

  async function toggleScheduleEnabled(rule: PresenceScheduleRule, enabled: boolean) {
    const nextSchedules = schedules.map((item) =>
      item.id === rule.id ? { ...item, enabled } : item
    );
    await persistPatch(
      { presence_schedules: nextSchedules },
      enabled ? `${rule.name} enabled.` : `${rule.name} paused.`
    );
  }

  async function removeSchedule(rule: PresenceScheduleRule) {
    const nextSchedules = schedules.filter((item) => item.id !== rule.id);
    await persistPatch({ presence_schedules: nextSchedules }, `${rule.name} deleted.`);
  }

  async function moveSchedule(index: number, delta: -1 | 1) {
    const target = index + delta;
    if (target < 0 || target >= schedules.length) return;
    const copy = [...schedules];
    const [moved] = copy.splice(index, 1);
    copy.splice(target, 0, moved);
    await persistPatch({ presence_schedules: copy }, 'Schedule priority updated.');
  }

  async function changeTimezone(nextTimezone: string) {
    if (!nextTimezone || nextTimezone === savedTimezone) return;
    await persistPatch({ presence_timezone: nextTimezone }, `Timezone set to ${nextTimezone}.`);
  }

  async function resetOverride() {
    await persistPatch({ presence_reset_override: true }, 'Returned to your scheduled status.');
  }

  async function skipActiveSchedule() {
    await persistPatch({ presence_skip_active_schedule: true }, 'Skipped current schedule window.');
  }
</script>

<div class="schedules-panel" class:compact>
  {#if currentState && (currentState.source === 'schedule' || currentState.source === 'override' || currentState.source === 'timed' || (currentState.effective_status === 'invisible' && currentState.suppress_notifications))}
    <div class="live-banner">
      <div class="live-banner-copy">
        <i class={`presence-symbol presence-${currentState.effective_status}`} aria-hidden="true"
        ></i>
        <span>
          {#if currentState.source === 'timed'}
            <strong
              >{presenceStatusLabel(currentState.effective_status)} · {formatTimestampHint(
                currentState.timed_expires_at,
                now
              )}</strong
            >
            <small>Manual timer takes priority over schedules until it ends.</small>
          {:else if currentState.source === 'schedule'}
            <strong
              >{currentState.active_schedule_name} is active ({presenceStatusLabel(
                currentState.effective_status
              )})</strong
            >
            <small
              >{formatTimestampHint(currentState.active_schedule_ends_at, now) || 'Active now'}.
              Skipping only affects this window.</small
            >
          {:else if currentState.source === 'override'}
            <strong
              >Skipping {currentState.skipped_schedule_name} · {formatTimestampHint(
                currentState.skipped_schedule_until,
                now
              )}</strong
            >
            <small>Your recurring schedule stays enabled for the next window.</small>
          {:else if currentState.effective_status === 'invisible' && currentState.suppress_notifications}
            <strong
              >Invisible · Notifications silenced by {currentState.active_schedule_name}</strong
            >
            <small>You remain offline to others while alerts stay muted.</small>
          {/if}
        </span>
      </div>
      {#if currentState.source === 'schedule'}
        <button
          type="button"
          class="secondary-button compact-btn"
          disabled={busy}
          onclick={() => void skipActiveSchedule()}
        >
          Skip now
        </button>
      {:else if currentState.source === 'override' || currentState.source === 'timed'}
        <button
          type="button"
          class="secondary-button compact-btn"
          disabled={busy}
          onclick={() => void resetOverride()}
        >
          Resume schedule
        </button>
      {/if}
    </div>
  {/if}

  <div class="timeline-card">
    <div class="timeline-header">
      <span class="timeline-title">24-hour preview</span>
      <div class="day-pills" role="group" aria-label="Preview day of week">
        {#each DAY_SHORT_LABELS as label, idx (idx)}
          <button
            type="button"
            class:selected={previewDay === idx}
            title={DAY_NAMES[idx]}
            aria-pressed={previewDay === idx}
            onclick={() => (selectedDay = idx)}
          >
            {label}
          </button>
        {/each}
      </div>
    </div>

    <div class="timeline-track-wrap">
      <div
        class="timeline-track"
        role="img"
        aria-label={`Scheduled status for ${DAY_NAMES[previewDay]}`}
      >
        {#each timelineSlices.slices as slice, idx (idx)}
          <span class={`timeline-slice slice-${slice.status}`} title={slice.label}></span>
        {/each}
      </div>
      {#if previewDay === todayIndex}
        <span class="timeline-now" style:left={`${nowPercent}%`} title="Current time"></span>
      {/if}
    </div>

    <div class="timeline-axis" aria-hidden="true">
      <span>12a</span>
      <span>6a</span>
      <span>12p</span>
      <span>6p</span>
      <span>12a</span>
    </div>

    <div class="timeline-footer">
      <span class="legend">
        <span><i class="swatch slice-online"></i>Online</span>
        <span><i class="swatch slice-idle"></i>Idle</span>
        <span><i class="swatch slice-dnd"></i>DND</span>
        <span><i class="swatch slice-invisible"></i>Invisible</span>
      </span>
      {#if timelineSlices.hasOverlap}
        <span class="overlap-note">Overlapping times use the top schedule first.</span>
      {/if}
    </div>
  </div>

  {#if editing}
    <form class="schedule-editor" onsubmit={saveEditor}>
      <div class="editor-header">
        <h3>{editingId ? 'Edit schedule' : 'New schedule'}</h3>
        <button
          type="button"
          class="secondary-button compact-btn"
          disabled={busy}
          onclick={() => {
            editing = false;
            editingId = null;
          }}
        >
          Cancel
        </button>
      </div>

      <label class="field">
        <span>Schedule name</span>
        <input
          type="text"
          bind:value={draftName}
          maxlength="32"
          placeholder="e.g. Sleep, Work Focus"
          disabled={busy}
          required
        />
      </label>

      <div class="field">
        <span>Status during this time</span>
        <div class="status-grid" role="radiogroup" aria-label="Scheduled status">
          {#each statusChoices as option (option.value)}
            <button
              type="button"
              role="radio"
              aria-checked={draftStatus === option.value}
              class:selected={draftStatus === option.value}
              disabled={busy}
              onclick={() => (draftStatus = option.value)}
            >
              <i class={`presence-symbol presence-${option.value}`} aria-hidden="true"></i>
              <span>{option.label}</span>
            </button>
          {/each}
        </div>
      </div>

      <div class="time-row">
        <label class="field">
          <span>Starts</span>
          <input type="time" bind:value={draftStart} disabled={busy} required />
        </label>
        <label class="field">
          <span>Ends</span>
          <input type="time" bind:value={draftEnd} disabled={busy} required />
        </label>
      </div>

      {#if draftSameTime}
        <p class="field-warning" role="alert">Start and end time must be different.</p>
      {:else if draftIsOvernight}
        <p class="overnight-note">
          Overnight schedule: ends at {formatMinuteOfDay(draftEndMinute)} the next morning.
        </p>
      {/if}

      <div class="field">
        <div class="days-header">
          <span>Repeat on</span>
          <div class="day-shortcuts">
            <button
              type="button"
              disabled={busy}
              onclick={() => (draftDays = [0, 1, 2, 3, 4, 5, 6])}
            >
              Every day
            </button>
            <button type="button" disabled={busy} onclick={() => (draftDays = [1, 2, 3, 4, 5])}>
              Weekdays
            </button>
            <button type="button" disabled={busy} onclick={() => (draftDays = [0, 6])}>
              Weekends
            </button>
          </div>
        </div>
        <div class="day-toggle-row" role="group" aria-label="Days of week">
          {#each DAY_SHORT_LABELS as label, idx (idx)}
            <button
              type="button"
              class:selected={draftDays.includes(idx)}
              aria-pressed={draftDays.includes(idx)}
              title={DAY_NAMES[idx]}
              disabled={busy}
              onclick={() => toggleDraftDay(idx)}
            >
              {label}
            </button>
          {/each}
        </div>
        {#if draftDays.length === 0}
          <p class="field-warning">Pick at least one day.</p>
        {/if}
      </div>

      <div class="editor-actions">
        <button type="submit" class="primary-button" disabled={busy || !draftValid}>
          {busy ? 'Saving…' : editingId ? 'Save changes' : 'Add schedule'}
        </button>
      </div>
    </form>
  {:else}
    <div class="toolbar">
      <div class="presets">
        <span class="presets-label">Quick add:</span>
        {#each presets as preset (preset.id)}
          <button
            type="button"
            class="preset-chip"
            disabled={busy || schedules.length >= MAX_PRESENCE_SCHEDULES}
            title={preset.hint}
            onclick={() => openNewEditor(preset)}
          >
            <i class={`presence-symbol presence-${preset.status}`} aria-hidden="true"></i>
            <span>{preset.label}</span>
          </button>
        {/each}
      </div>
      <button
        type="button"
        class="primary-button compact-btn"
        disabled={busy || schedules.length >= MAX_PRESENCE_SCHEDULES}
        onclick={() => openNewEditor()}
      >
        <Icon name="plus" size={15} />Custom schedule
      </button>
    </div>

    {#if schedules.length === 0}
      <div class="empty-schedules">
        <p>No presence schedules yet.</p>
        <small>
          Automatically switch to Do Not Disturb while you sleep or Idle during work hours.
        </small>
      </div>
    {:else}
      <div class="schedule-list">
        {#each schedules as rule, index (rule.id)}
          <div class="schedule-card" class:paused={!rule.enabled}>
            <div class="schedule-main">
              <i class={`presence-symbol presence-${rule.status}`} aria-hidden="true"></i>
              <div class="schedule-copy">
                <div class="schedule-title-row">
                  <strong>{rule.name}</strong>
                  <span class="status-tag">{presenceStatusLabel(rule.status)}</span>
                </div>
                <small>
                  {formatDaysSummary(rule.days)} · {formatMinuteOfDay(rule.start_minute)} – {formatMinuteOfDay(
                    rule.end_minute
                  )}{rule.end_minute < rule.start_minute ? ' (next day)' : ''}
                </small>
              </div>
            </div>
            <div class="schedule-controls">
              {#if schedules.length > 1}
                <button
                  type="button"
                  class="icon-btn"
                  aria-label={`Move ${rule.name} higher priority`}
                  title="Higher priority"
                  disabled={busy || index === 0}
                  onclick={() => void moveSchedule(index, -1)}
                >
                  ↑
                </button>
                <button
                  type="button"
                  class="icon-btn"
                  aria-label={`Move ${rule.name} lower priority`}
                  title="Lower priority"
                  disabled={busy || index === schedules.length - 1}
                  onclick={() => void moveSchedule(index, 1)}
                >
                  ↓
                </button>
              {/if}
              <button
                type="button"
                class="secondary-button compact-btn"
                disabled={busy}
                onclick={() => openEditEditor(rule)}
              >
                Edit
              </button>
              <button
                type="button"
                class="icon-btn danger"
                aria-label={`Delete ${rule.name}`}
                title="Delete schedule"
                disabled={busy}
                onclick={() => void removeSchedule(rule)}
              >
                <Icon name="trash" size={15} />
              </button>
              <label class="switch" title={rule.enabled ? 'Pause schedule' : 'Enable schedule'}>
                <input
                  type="checkbox"
                  checked={rule.enabled}
                  disabled={busy}
                  aria-label={`Enable ${rule.name}`}
                  onchange={(event) =>
                    void toggleScheduleEnabled(rule, event.currentTarget.checked)}
                />
                <span class="switch-track"></span>
              </label>
            </div>
          </div>
        {/each}
      </div>
    {/if}

    <div class="timezone-row">
      <label class="tz-select-label">
        <span>Timezone</span>
        <select
          value={savedTimezone}
          disabled={busy}
          onchange={(event) => void changeTimezone(event.currentTarget.value)}
        >
          {#each timezoneChoices as zone (zone)}
            <option value={zone}>{zone}</option>
          {/each}
        </select>
      </label>
      {#if timezoneDiffersFromDevice}
        <button
          type="button"
          class="secondary-button compact-btn"
          disabled={busy}
          onclick={() => void changeTimezone(deviceTimezone)}
        >
          Sync to {deviceTimezone}
        </button>
      {/if}
    </div>
  {/if}

  {#if error}
    <p class="form-error" role="alert">{error}</p>
  {/if}
  {#if notice}
    <p class="feedback" role="status">{notice}</p>
  {/if}
</div>

<style>
  .schedules-panel {
    display: grid;
    gap: 14px;
  }

  .compact-btn {
    min-height: 34px;
    padding: 6px 12px;
    border-radius: 9px;
    font-size: 0.8rem;
    font-weight: 600;
  }

  .live-banner {
    --presence-cutout: var(--surface-subtle);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface-subtle);
  }

  .live-banner-copy {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1 1 200px;
  }

  .live-banner-copy > span {
    display: grid;
    gap: 2px;
  }

  .live-banner-copy strong {
    font-size: 0.84rem;
    font-weight: 650;
    line-height: 1.3;
  }

  .live-banner-copy small {
    color: var(--text-muted);
    font-size: 0.75rem;
    line-height: 1.35;
  }

  .timeline-card {
    display: grid;
    gap: 10px;
    padding: 14px;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: var(--surface-subtle);
  }

  .timeline-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .timeline-title {
    font-size: 0.82rem;
    font-weight: 700;
    color: var(--text);
  }

  .day-pills {
    display: inline-flex;
    gap: 5px;
  }

  .day-pills button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--surface-raised);
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    transition:
      background 110ms ease,
      color 110ms ease,
      border-color 110ms ease;
  }

  .day-pills button:hover:not(.selected) {
    background: var(--surface-hover);
    color: var(--text);
  }

  .day-pills button.selected {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--on-accent);
  }

  .timeline-track-wrap {
    position: relative;
    padding: 3px 0;
  }

  .timeline-track {
    display: flex;
    height: 18px;
    width: 100%;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--surface-raised);
  }

  .timeline-slice {
    flex: 1 1 0;
    min-width: 0;
  }

  .slice-online {
    background: rgb(59 165 93 / 38%);
  }

  .slice-idle {
    background: rgb(240 166 27 / 86%);
  }

  .slice-dnd {
    background: rgb(237 66 69 / 88%);
  }

  .slice-invisible {
    background: rgb(116 127 141 / 82%);
  }

  .timeline-now {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2.5px;
    background: var(--text);
    border-radius: 2px;
    box-shadow: 0 0 0 1px var(--surface-subtle);
    pointer-events: none;
  }

  .timeline-axis {
    display: flex;
    justify-content: space-between;
    color: var(--text-muted);
    font-size: 0.71rem;
    font-family: var(--font-mono, monospace);
  }

  .timeline-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    font-size: 0.73rem;
    color: var(--text-muted);
  }

  .legend {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }

  .legend > span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 3px;
  }

  .overlap-note {
    color: var(--text-soft);
    font-weight: 600;
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .presets {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }

  .presets-label {
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 600;
    margin-right: 2px;
  }

  .preset-chip {
    --presence-cutout: var(--surface-subtle);
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-height: 34px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 6px 11px;
    background: var(--surface-subtle);
    color: var(--text-soft);
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    transition:
      background 110ms ease,
      color 110ms ease,
      border-color 110ms ease;
  }

  .preset-chip:hover:not(:disabled) {
    --presence-cutout: var(--surface-hover);
    border-color: var(--line-strong);
    background: var(--surface-hover);
    color: var(--text);
  }

  .preset-chip:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toolbar .primary-button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .empty-schedules {
    display: grid;
    gap: 5px;
    padding: 20px 16px;
    border: 1px dashed var(--line);
    border-radius: 13px;
    text-align: center;
    color: var(--text-muted);
  }

  .empty-schedules p {
    margin: 0;
    font-size: 0.88rem;
    font-weight: 650;
    color: var(--text-soft);
  }

  .empty-schedules small {
    font-size: 0.78rem;
    line-height: 1.4;
  }

  .schedule-list {
    display: grid;
    gap: 10px;
  }

  .schedule-card {
    --presence-cutout: var(--surface-subtle);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px 14px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: var(--surface-subtle);
  }

  .schedule-card.paused {
    opacity: 0.65;
  }

  .schedule-main {
    display: flex;
    align-items: center;
    gap: 11px;
    min-width: 0;
    flex: 1 1 200px;
  }

  .schedule-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .schedule-title-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
  }

  .schedule-title-row strong {
    font-size: 0.88rem;
    font-weight: 700;
    line-height: 1.25;
  }

  .status-tag {
    border-radius: 999px;
    padding: 2px 8px;
    background: var(--surface-hover);
    color: var(--text-soft);
    font-size: 0.72rem;
    font-weight: 600;
    line-height: 1.25;
  }

  .schedule-copy small {
    color: var(--text-muted);
    font-size: 0.76rem;
    line-height: 1.35;
  }

  .schedule-controls {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
  }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface-raised);
    color: var(--text-soft);
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    transition:
      background 110ms ease,
      color 110ms ease,
      border-color 110ms ease;
  }

  .icon-btn:hover:not(:disabled) {
    border-color: var(--line-strong);
    background: var(--surface-hover);
    color: var(--text);
  }

  .icon-btn.danger:hover:not(:disabled) {
    color: var(--danger, #ed4245);
  }

  .icon-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .switch {
    position: relative;
    display: inline-flex;
    align-items: center;
    cursor: pointer;
    margin-left: 4px;
  }

  .switch input {
    position: absolute;
    opacity: 0;
    width: 1px;
    height: 1px;
  }

  .switch-track {
    position: relative;
    width: 38px;
    height: 22px;
    border-radius: 999px;
    background: var(--line);
    transition: background 120ms ease;
  }

  .switch-track::after {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--surface-raised);
    box-shadow: 0 1px 2px rgb(0 0 0 / 25%);
    transition: transform 120ms ease;
    content: '';
  }

  .switch input:checked + .switch-track {
    background: var(--accent);
  }

  .switch input:checked + .switch-track::after {
    transform: translateX(16px);
  }

  .switch input:focus-visible + .switch-track {
    outline: 3px solid var(--focus);
    outline-offset: 2px;
  }

  .schedule-editor {
    display: grid;
    gap: 14px;
    padding: 16px;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: var(--surface-subtle);
  }

  .editor-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .editor-header h3 {
    margin: 0;
    font-size: 0.98rem;
  }

  .field {
    display: grid;
    gap: 7px;
  }

  .field > span,
  .days-header > span {
    font-size: 0.8rem;
    font-weight: 650;
    color: var(--text-soft);
  }

  .field input[type='text'],
  .field input[type='time'],
  .tz-select-label select {
    min-height: 38px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 8px 11px;
    background: var(--surface-raised);
    color: var(--text);
    font-size: 0.84rem;
  }

  .status-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .status-grid button {
    --presence-cutout: var(--surface-raised);
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 40px;
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 9px;
    background: var(--surface-raised);
    color: var(--text-soft);
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    text-align: left;
    transition:
      background 110ms ease,
      color 110ms ease,
      border-color 110ms ease;
  }

  .status-grid button:hover:not(:disabled) {
    --presence-cutout: var(--surface-hover);
    background: var(--surface-hover);
    color: var(--text);
  }

  .status-grid button.selected {
    --presence-cutout: var(--accent-soft);
    border-color: var(--accent);
    background: var(--accent-soft);
    color: var(--text);
  }

  .time-row {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .overnight-note {
    margin: -4px 0 0;
    padding: 8px 11px;
    border-radius: 9px;
    background: var(--surface-hover);
    color: var(--text-soft);
    font-size: 0.78rem;
    line-height: 1.35;
  }

  .field-warning {
    margin: 0;
    color: var(--danger, #ed4245);
    font-size: 0.78rem;
    font-weight: 500;
  }

  .days-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .day-shortcuts {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 5px;
  }

  .day-shortcuts button {
    min-height: 28px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 4px 9px;
    background: var(--surface-raised);
    color: var(--text-soft);
    font-size: 0.73rem;
    font-weight: 600;
    cursor: pointer;
  }

  .day-shortcuts button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }

  .day-toggle-row {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }

  .day-toggle-row button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--surface-raised);
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 700;
    cursor: pointer;
  }

  .day-toggle-row button:hover:not(.selected) {
    background: var(--surface-hover);
    color: var(--text);
  }

  .day-toggle-row button.selected {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--on-accent);
  }

  .editor-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: 2px;
  }

  .timezone-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-top: 6px;
    border-top: 1px solid var(--line-soft);
  }

  .tz-select-label {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-muted);
  }

  .presence-symbol {
    position: relative;
    display: inline-block;
    width: 12px;
    height: 12px;
    flex: 0 0 auto;
    border-radius: 50%;
    background: #747f8d;
  }

  .presence-online {
    background: #3ba55d;
  }

  .presence-idle {
    background: #f0a61b;
  }

  .presence-idle::after {
    position: absolute;
    top: -2px;
    left: -2px;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--presence-cutout, var(--surface-subtle));
    content: '';
  }

  .presence-dnd {
    background: #ed4245;
  }

  .presence-dnd::after {
    position: absolute;
    top: 4.75px;
    left: 2.5px;
    width: 7px;
    height: 2.5px;
    border-radius: 2px;
    background: var(--presence-cutout, var(--surface-subtle));
    content: '';
  }

  .presence-invisible {
    box-shadow: inset 0 0 0 2.75px #747f8d;
    background: transparent;
  }

  .feedback {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.8rem;
  }

  .form-error {
    margin: 0;
    font-size: 0.8rem;
  }

  @media (max-width: 460px) {
    .time-row {
      grid-template-columns: 1fr;
    }

    .toolbar {
      align-items: stretch;
    }

    .toolbar .primary-button {
      width: 100%;
    }
  }
</style>
