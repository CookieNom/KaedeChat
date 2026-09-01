<script lang="ts">
  import { entityKey, entityRef } from '$lib/chat/refs';
  import type { GuildMemberSummary, UserSummary } from '$lib/chat/types';
  import type {
    CreateTrackerTaskRequest,
    TrackerLane,
    TrackerPriority,
    TrackerTask,
    UpdateTrackerTaskRequest
  } from '$lib/task-tracker/types';
  import { trackerCanChangeAssignee } from '$lib/task-tracker/board';
  import { portal } from '$lib/ui/portal';
  import { onMount, untrack } from 'svelte';
  import Icon from './Icon.svelte';

  let {
    task = null,
    initialLane,
    lanes,
    members,
    currentUser = null,
    canAssign = false,
    canDelete = false,
    assignmentOnly = false,
    readOnly = false,
    busy = false,
    error = '',
    onSave,
    onDelete,
    onClose
  }: {
    task?: TrackerTask | null;
    initialLane: TrackerLane;
    lanes: TrackerLane[];
    members: GuildMemberSummary[];
    currentUser?: UserSummary | null;
    canAssign?: boolean;
    canDelete?: boolean;
    assignmentOnly?: boolean;
    readOnly?: boolean;
    busy?: boolean;
    error?: string;
    onSave: (
      request: CreateTrackerTaskRequest | UpdateTrackerTaskRequest,
      lane: TrackerLane
    ) => Promise<void> | void;
    onDelete?: () => Promise<void> | void;
    onClose: () => void;
  } = $props();

  const initialTask = untrack(() => task);
  const initialTargetLane = untrack(() => initialLane);
  let title = $state(initialTask?.title ?? '');
  let description = $state(initialTask?.description ?? '');
  let priority = $state<TrackerPriority>(initialTask?.priority ?? 'none');
  let laneKey = $state(
    initialTask ? `${initialTask.lane_id}@${initialTask.lane_domain}` : entityKey(initialTargetLane)
  );
  let assigneeKey = $state(initialTask?.assignee ? entityKey(initialTask.assignee) : '');
  let due = $state(toLocalDateTime(initialTask?.due_at ?? null));
  let confirmDelete = $state(false);
  let dialog = $state<HTMLElement | null>(null);
  let titleInput = $state<HTMLInputElement | null>(null);

  const assignableMembers = $derived.by(() => {
    const unique: Record<string, GuildMemberSummary> = {};
    for (const member of members) unique[entityKey(member.user)] = member;
    if (currentUser && !unique[entityKey(currentUser)]) {
      unique[entityKey(currentUser)] = {
        guild_id: '',
        guild_domain: initialLane.channel_domain,
        user: currentUser,
        nickname: null,
        role_ids: []
      };
    }
    if (task?.assignee && !unique[entityKey(task.assignee)]) {
      unique[entityKey(task.assignee)] = {
        guild_id: initialLane.channel_id,
        guild_domain: initialLane.channel_domain,
        user: task.assignee,
        nickname: null,
        role_ids: []
      };
    }
    return Object.values(unique)
      .filter(
        (member) =>
          canAssign ||
          (currentUser && entityKey(member.user) === entityKey(currentUser)) ||
          (task?.assignee && entityKey(member.user) === entityKey(task.assignee))
      )
      .sort((left, right) => memberName(left).localeCompare(memberName(right)));
  });
  const canChangeAssignee = $derived(trackerCanChangeAssignee(task, currentUser, canAssign));

  onMount(() => {
    if (titleInput?.disabled) dialog?.focus();
    else titleInput?.focus();
  });

  function memberName(member: GuildMemberSummary): string {
    return member.nickname?.trim() || member.user.display_name?.trim() || member.user.username;
  }

  function toLocalDateTime(value: string | null): string {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return '';
    const local = new Date(date.valueOf() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }

  function dueAt(): string | null {
    if (!due) return null;
    const value = new Date(due);
    return Number.isNaN(value.valueOf()) ? null : value.toISOString();
  }

  function selectedLane(): TrackerLane {
    return lanes.find((lane) => entityKey(lane) === laneKey) ?? initialLane;
  }

  function submit() {
    if (busy || readOnly || (!assignmentOnly && !title.trim())) return;
    if (assignmentOnly) {
      void onSave({ assignee_id: assigneeKey || null }, initialLane);
      return;
    }
    const common = {
      title: title.trim(),
      description: description.trim() || null,
      priority,
      due_at: dueAt(),
      ...(canChangeAssignee ? { assignee_id: assigneeKey || null } : {})
    };
    const request: CreateTrackerTaskRequest | UpdateTrackerTaskRequest = task
      ? common
      : { ...common, lane_id: entityRef(selectedLane()) };
    void onSave(request, selectedLane());
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
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
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

<div class="task-dialog-layer" use:portal>
  <button
    class="task-dialog-backdrop"
    type="button"
    aria-label="Close task editor"
    disabled={busy}
    onclick={onClose}
  ></button>
  <div
    bind:this={dialog}
    class="task-dialog"
    role="dialog"
    tabindex="-1"
    aria-modal="true"
    aria-labelledby="task-dialog-title"
    aria-busy={busy}
  >
    <header>
      <div>
        <span>{task ? task.key : 'New task'}</span>
        <h2 id="task-dialog-title">
          {readOnly
            ? 'Task details'
            : assignmentOnly
              ? 'Assign task'
              : task
                ? 'Edit task'
                : 'Create task'}
        </h2>
      </div>
      <button type="button" disabled={busy} aria-label="Close" onclick={onClose}>
        <Icon name="x" size={20} />
      </button>
    </header>

    <form
      onsubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label class="wide-field">
        <span>Title</span>
        <input
          bind:this={titleInput}
          bind:value={title}
          maxlength="200"
          autocomplete="off"
          required
          disabled={busy || readOnly || assignmentOnly}
        />
      </label>
      <label class="wide-field">
        <span>Description</span>
        <textarea
          bind:value={description}
          maxlength="10000"
          rows="5"
          placeholder="Add context, acceptance criteria, or links…"
          disabled={busy || readOnly || assignmentOnly}
        ></textarea>
      </label>
      <label>
        <span>Status</span>
        <select bind:value={laneKey} disabled={busy || readOnly || assignmentOnly}>
          {#each lanes as lane (entityKey(lane))}
            <option value={entityKey(lane)}>{lane.name}</option>
          {/each}
        </select>
      </label>
      <label>
        <span>Priority</span>
        <select bind:value={priority} disabled={busy || readOnly || assignmentOnly}>
          <option value="none">No priority</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
        </select>
      </label>
      <label>
        <span>Due date</span>
        <input
          bind:value={due}
          type="datetime-local"
          disabled={busy || readOnly || assignmentOnly}
        />
      </label>
      <label>
        <span>Assignee</span>
        <select bind:value={assigneeKey} disabled={busy || readOnly || !canChangeAssignee}>
          <option value="">Unassigned</option>
          {#each assignableMembers as member (entityKey(member.user))}
            <option value={entityKey(member.user)}>{memberName(member)}</option>
          {/each}
        </select>
        {#if !readOnly && !canAssign}
          <small>
            {canChangeAssignee
              ? 'You can assign this task to yourself; assigning others requires additional permission.'
              : 'You do not have permission to change this assignee.'}
          </small>
        {/if}
      </label>

      {#if error}<p class="task-dialog-error wide-field" role="alert">{error}</p>{/if}

      <footer class="wide-field">
        <div>
          {#if task && canDelete && !readOnly && !assignmentOnly && onDelete}
            {#if confirmDelete}
              <span class="delete-confirm">Delete this task permanently?</span>
              <button
                class="danger-button"
                type="button"
                disabled={busy}
                onclick={() => void onDelete?.()}>Confirm delete</button
              >
              <button type="button" disabled={busy} onclick={() => (confirmDelete = false)}
                >Keep task</button
              >
            {:else}
              <button
                class="delete-button"
                type="button"
                disabled={busy}
                onclick={() => (confirmDelete = true)}
              >
                <Icon name="trash" size={16} />Delete
              </button>
            {/if}
          {/if}
        </div>
        <div>
          <button type="button" disabled={busy} onclick={onClose}
            >{readOnly ? 'Close' : 'Cancel'}</button
          >
          {#if !readOnly}
            <button class="save-button" disabled={busy || (!assignmentOnly && !title.trim())}>
              {busy
                ? 'Saving…'
                : assignmentOnly
                  ? 'Save assignee'
                  : task
                    ? 'Save changes'
                    : 'Create task'}
            </button>
          {/if}
        </div>
      </footer>
    </form>
  </div>
</div>

<style>
  .task-dialog-layer {
    position: fixed;
    z-index: 1200;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }

  .task-dialog-backdrop {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: rgb(4 8 15 / 72%);
    backdrop-filter: blur(3px);
  }

  .task-dialog {
    position: relative;
    width: min(680px, 100%);
    max-height: min(760px, calc(100dvh - 2rem));
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  header {
    position: sticky;
    z-index: 2;
    top: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line-soft);
    padding: 1rem 1.1rem;
    background: color-mix(in srgb, var(--surface-raised) 96%, transparent);
    backdrop-filter: blur(12px);
  }

  header span {
    color: var(--text-muted);
    font-family: var(--font-mono);
    font-size: 0.68rem;
    font-weight: 760;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  h2 {
    margin: 0.15rem 0 0;
    font-size: 1.25rem;
  }

  header > button,
  footer button {
    display: inline-flex;
    min-height: 38px;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    border: 0;
    border-radius: 9px;
    padding: 0 0.8rem;
    color: var(--text-soft);
    background: transparent;
    font: inherit;
    font-weight: 720;
    cursor: pointer;
  }

  header > button {
    width: 40px;
    padding: 0;
  }

  header > button:hover,
  footer button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }

  form {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
    padding: 1.1rem;
  }

  label {
    display: grid;
    min-width: 0;
    align-content: start;
    gap: 0.45rem;
    color: var(--text-soft);
    font-size: 0.74rem;
    font-weight: 720;
  }

  .wide-field {
    grid-column: 1 / -1;
  }

  input,
  textarea,
  select {
    width: 100%;
    min-height: 42px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.65rem 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
    font: inherit;
    font-weight: 500;
  }

  textarea {
    min-height: 130px;
    resize: vertical;
    line-height: 1.45;
  }

  label small {
    color: var(--text-muted);
    font-size: 0.66rem;
    font-weight: 500;
  }

  .task-dialog-error {
    margin: 0;
    border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--line));
    border-radius: 9px;
    padding: 0.7rem;
    color: var(--danger);
    background: var(--danger-soft);
    font-size: 0.75rem;
  }

  footer,
  footer > div {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
  }

  footer {
    justify-content: space-between;
    border-top: 1px solid var(--line-soft);
    padding-top: 1rem;
  }

  footer > div:last-child {
    margin-left: auto;
  }

  footer .save-button {
    color: var(--on-accent);
    background: var(--accent);
  }

  footer .delete-button,
  footer .danger-button,
  .delete-confirm {
    color: var(--danger);
  }

  footer .danger-button {
    background: var(--danger-soft);
  }

  .delete-confirm {
    font-size: 0.72rem;
    font-weight: 720;
  }

  button:disabled,
  input:disabled,
  textarea:disabled,
  select:disabled {
    cursor: not-allowed;
    opacity: 0.58;
  }

  @media (max-width: 620px) {
    .task-dialog-layer {
      align-items: end;
      padding: 0;
    }

    .task-dialog {
      width: 100%;
      max-height: 92dvh;
      border-radius: 18px 18px 0 0;
    }

    form {
      grid-template-columns: minmax(0, 1fr);
      padding-bottom: calc(1rem + env(safe-area-inset-bottom));
    }

    .wide-field {
      grid-column: 1;
    }

    footer,
    footer > div {
      align-items: stretch;
      flex-direction: column;
    }

    footer > div:last-child {
      width: 100%;
      margin-left: 0;
    }
  }
</style>
