<script lang="ts">
  import { entityKey } from '$lib/chat/refs';
  import { trackerColor } from '$lib/task-tracker/board';
  import type {
    CreateTrackerLaneRequest,
    TrackerBoard,
    TrackerLane,
    UpdateTrackerLaneRequest
  } from '$lib/task-tracker/types';
  import { portal } from '$lib/ui/portal';
  import { onMount, untrack } from 'svelte';
  import Icon from './Icon.svelte';

  let {
    board,
    lanes,
    busy = false,
    error = '',
    onPrefix,
    onCreateLane,
    onUpdateLane,
    onMoveLane,
    onDeleteLane,
    onClose
  }: {
    board: TrackerBoard;
    lanes: TrackerLane[];
    busy?: boolean;
    error?: string;
    onPrefix: (prefix: string) => Promise<void> | void;
    onCreateLane: (request: CreateTrackerLaneRequest) => Promise<void> | void;
    onUpdateLane: (lane: TrackerLane, patch: UpdateTrackerLaneRequest) => Promise<void> | void;
    onMoveLane: (lane: TrackerLane, position: number) => Promise<void> | void;
    onDeleteLane: (lane: TrackerLane) => Promise<void> | void;
    onClose: () => void;
  } = $props();

  const initialBoard = untrack(() => board);
  const initialLanes = untrack(() => lanes);
  let prefix = $state(initialBoard.key_prefix);
  let laneName = $state('');
  let laneColor = $state('#3b82f6');
  let laneCompleted = $state(false);
  let names = $state<Record<string, string>>(
    Object.fromEntries(initialLanes.map((lane) => [entityKey(lane), lane.name]))
  );
  let colors = $state<Record<string, string>>(
    Object.fromEntries(initialLanes.map((lane) => [entityKey(lane), trackerColor(lane.color)]))
  );
  let completed = $state<Record<string, boolean>>(
    Object.fromEntries(initialLanes.map((lane) => [entityKey(lane), lane.completed]))
  );
  let deleteConfirmKey = $state('');
  let dialog = $state<HTMLElement | null>(null);
  let prefixInput = $state<HTMLInputElement | null>(null);

  $effect(() => {
    for (const lane of lanes) {
      const key = entityKey(lane);
      if (!(key in names)) names = { ...names, [key]: lane.name };
      if (!(key in colors)) colors = { ...colors, [key]: trackerColor(lane.color) };
      if (!(key in completed)) completed = { ...completed, [key]: lane.completed };
    }
  });

  onMount(() => prefixInput?.focus());

  function parseColor(value: string): number {
    return Number.parseInt(value.replace(/^#/, ''), 16);
  }

  function addLane() {
    if (!laneName.trim() || busy) return;
    void onCreateLane({
      name: laneName.trim(),
      color: parseColor(laneColor),
      completed: laneCompleted
    });
    laneName = '';
    laneCompleted = false;
  }

  function saveLane(lane: TrackerLane) {
    const key = entityKey(lane);
    const name = names[key]?.trim();
    if (!name || busy) return;
    void onUpdateLane(lane, {
      name,
      color: parseColor(colors[key] ?? trackerColor(lane.color)),
      completed: completed[key] ?? lane.completed
    });
  }

  function deleteLaneBlockReason(lane: TrackerLane): string {
    if (lane.task_count > 0) return 'Move or delete every task first';
    if (lanes.length <= 1) return 'A tracker must keep at least one status';
    return '';
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && !busy) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== 'Tab' || !dialog) return;
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
</script>

<svelte:window onkeydown={keydown} />

<div class="tracker-settings-layer" use:portal>
  <button
    class="tracker-settings-backdrop"
    type="button"
    aria-label="Close tracker settings"
    disabled={busy}
    onclick={onClose}
  ></button>
  <div
    bind:this={dialog}
    class="tracker-settings-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="tracker-settings-title"
    aria-busy={busy}
  >
    <header>
      <div>
        <span>Task tracker</span>
        <h2 id="tracker-settings-title">Board settings</h2>
      </div>
      <button type="button" disabled={busy} aria-label="Close" onclick={onClose}>
        <Icon name="x" size={20} />
      </button>
    </header>

    <div class="tracker-settings-content">
      <form
        class="prefix-form"
        onsubmit={(event) => {
          event.preventDefault();
          if (prefix.trim()) void onPrefix(prefix.trim().toUpperCase());
        }}
      >
        <label>
          <span>Task key prefix</span>
          <input
            bind:this={prefixInput}
            bind:value={prefix}
            minlength="2"
            maxlength="10"
            pattern="[A-Za-z][A-Za-z0-9]*"
            autocomplete="off"
            required
            disabled={busy}
          />
          <small
            >Used for task keys such as {prefix.trim().toUpperCase() || 'TASK'}-42. Changing it
            updates existing keys, while task numbers stay the same.</small
          >
        </label>
        <button
          disabled={busy || !prefix.trim() || prefix.trim().toUpperCase() === board.key_prefix}
          >Save prefix</button
        >
      </form>

      <section class="lane-settings" aria-labelledby="lane-settings-title">
        <div class="section-heading">
          <div>
            <h3 id="lane-settings-title">Statuses</h3>
            <p>Statuses are ordered from the start of work to completion.</p>
          </div>
          <span>{lanes.length}</span>
        </div>
        <div class="lane-list">
          {#each lanes as lane, index (entityKey(lane))}
            {@const key = entityKey(lane)}
            {@const deleteBlocked = deleteLaneBlockReason(lane)}
            <article>
              <input
                class="lane-color"
                bind:value={colors[key]}
                type="color"
                aria-label={`Color for ${lane.name}`}
                disabled={busy}
              />
              <label class="lane-name">
                <span class="visually-hidden">Status name</span>
                <input bind:value={names[key]} maxlength="100" required disabled={busy} />
              </label>
              <label class="completed-toggle">
                <input bind:checked={completed[key]} type="checkbox" disabled={busy} />
                Completed
              </label>
              <div class="lane-actions">
                <button
                  type="button"
                  aria-label={`Move ${lane.name} up`}
                  title="Move up"
                  disabled={busy || index === 0}
                  onclick={() => void onMoveLane(lane, index - 1)}>↑</button
                >
                <button
                  type="button"
                  aria-label={`Move ${lane.name} down`}
                  title="Move down"
                  disabled={busy || index === lanes.length - 1}
                  onclick={() => void onMoveLane(lane, index + 1)}>↓</button
                >
                <button type="button" disabled={busy} onclick={() => saveLane(lane)}>Save</button>
                {#if deleteConfirmKey === key}
                  <button
                    class="danger"
                    type="button"
                    disabled={busy || Boolean(deleteBlocked)}
                    title={deleteBlocked || 'Confirm delete'}
                    onclick={() => void onDeleteLane(lane)}>Confirm</button
                  >
                  <button type="button" disabled={busy} onclick={() => (deleteConfirmKey = '')}
                    >Keep</button
                  >
                {:else}
                  <button
                    class="icon-action danger"
                    type="button"
                    aria-label={`Delete ${lane.name}`}
                    disabled={busy || Boolean(deleteBlocked)}
                    title={deleteBlocked || 'Delete status'}
                    onclick={() => (deleteConfirmKey = key)}
                  >
                    <Icon name="trash" size={15} />
                  </button>
                {/if}
              </div>
            </article>
          {/each}
        </div>

        <form
          class="new-lane-form"
          onsubmit={(event) => {
            event.preventDefault();
            addLane();
          }}
        >
          <input
            bind:value={laneColor}
            class="lane-color"
            type="color"
            aria-label="New status color"
            disabled={busy}
          />
          <label>
            <span class="visually-hidden">New status name</span>
            <input
              bind:value={laneName}
              maxlength="100"
              placeholder="New status"
              autocomplete="off"
              required
              disabled={busy}
            />
          </label>
          <label class="completed-toggle">
            <input bind:checked={laneCompleted} type="checkbox" disabled={busy} />Completed
          </label>
          <button disabled={busy || !laneName.trim()}>
            <Icon name="plus" size={16} />Add status
          </button>
        </form>
      </section>

      {#if error}<p class="settings-error" role="alert">{error}</p>{/if}
    </div>
  </div>
</div>

<style>
  .tracker-settings-layer {
    position: fixed;
    z-index: 1200;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }

  .tracker-settings-backdrop {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: rgb(4 8 15 / 72%);
    backdrop-filter: blur(3px);
  }

  .tracker-settings-dialog {
    position: relative;
    width: min(820px, 100%);
    max-height: min(820px, calc(100dvh - 2rem));
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  header,
  .section-heading,
  .prefix-form,
  .new-lane-form,
  article,
  .lane-actions {
    display: flex;
    align-items: center;
  }

  header {
    position: sticky;
    z-index: 3;
    top: 0;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line-soft);
    padding: 1rem 1.1rem;
    background: color-mix(in srgb, var(--surface-raised) 96%, transparent);
    backdrop-filter: blur(12px);
  }

  header span {
    color: var(--text-muted);
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h2,
  h3,
  p {
    margin: 0;
  }

  h2 {
    margin-top: 0.15rem;
    font-size: 1.25rem;
  }

  header > button,
  .prefix-form > button,
  .new-lane-form > button,
  .lane-actions button {
    display: inline-flex;
    min-height: 36px;
    align-items: center;
    justify-content: center;
    gap: 0.35rem;
    border: 0;
    border-radius: 8px;
    padding: 0 0.7rem;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font: inherit;
    font-size: 0.72rem;
    font-weight: 730;
    cursor: pointer;
  }

  header > button,
  .lane-actions .icon-action {
    width: 38px;
    padding: 0;
    background: transparent;
  }

  .tracker-settings-content {
    display: grid;
    gap: 1.25rem;
    padding: 1.1rem;
  }

  .prefix-form {
    align-items: end;
    gap: 0.75rem;
    border: 1px solid var(--line-soft);
    border-radius: 12px;
    padding: 0.9rem;
    background: var(--surface-subtle);
  }

  label {
    display: grid;
    min-width: 0;
    flex: 1;
    gap: 0.4rem;
    color: var(--text-soft);
    font-size: 0.72rem;
    font-weight: 720;
  }

  label small,
  .section-heading p {
    color: var(--text-muted);
    font-size: 0.67rem;
    font-weight: 500;
  }

  input:not([type='checkbox']):not([type='color']) {
    width: 100%;
    min-height: 40px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0 0.7rem;
    color: var(--text);
    background: var(--surface-raised);
    font: inherit;
  }

  .lane-settings {
    display: grid;
    gap: 0.75rem;
  }

  .section-heading {
    justify-content: space-between;
    gap: 1rem;
  }

  .section-heading > span {
    display: grid;
    min-width: 30px;
    min-height: 26px;
    place-items: center;
    border-radius: 7px;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 0.7rem;
    font-weight: 800;
  }

  .lane-list {
    display: grid;
    gap: 0.45rem;
  }

  article,
  .new-lane-form {
    min-width: 0;
    gap: 0.65rem;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    padding: 0.65rem;
    background: var(--surface-subtle);
  }

  .lane-color {
    width: 38px;
    height: 38px;
    flex: 0 0 auto;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 3px;
    background: var(--surface-raised);
    cursor: pointer;
  }

  .lane-name {
    min-width: 130px;
  }

  .completed-toggle {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
  }

  .lane-actions {
    flex: 0 1 auto;
    justify-content: flex-end;
    gap: 0.3rem;
    margin-left: auto;
  }

  .lane-actions .danger,
  .settings-error {
    color: var(--danger);
  }

  .new-lane-form > label:not(.completed-toggle) {
    min-width: 150px;
  }

  .settings-error {
    border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--line));
    border-radius: 9px;
    padding: 0.7rem;
    background: var(--danger-soft);
    font-size: 0.75rem;
  }

  button:disabled,
  input:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  @media (max-width: 720px) {
    .tracker-settings-layer {
      align-items: end;
      padding: 0;
    }

    .tracker-settings-dialog {
      width: 100%;
      max-height: 94dvh;
      border-radius: 18px 18px 0 0;
    }

    .prefix-form,
    article,
    .new-lane-form {
      align-items: stretch;
      flex-wrap: wrap;
    }

    .prefix-form > label,
    .lane-name,
    .new-lane-form > label:not(.completed-toggle) {
      min-width: calc(100% - 50px);
    }

    .lane-actions {
      width: 100%;
      justify-content: flex-start;
      margin-left: 48px;
    }
  }
</style>
