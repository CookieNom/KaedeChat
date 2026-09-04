<script lang="ts">
  import { ApiError, userErrorMessage } from '$lib/api/client';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import type { Channel, GuildMemberSummary, UserSummary } from '$lib/chat/types';
  import { assetUrl } from '$lib/media/assets';
  import {
    filterTrackerTasks,
    moveTaskInBoard,
    orderedTrackerLanes,
    TrackerPermission,
    trackerColor,
    trackerDispatchRequiresRefresh,
    trackerDropPosition,
    trackerHasPermission,
    trackerCanChangeAssignee,
    trackerTaskEditMode,
    trackerTaskBelongsToUser,
    trackerTasksForLane
  } from '$lib/task-tracker/board';
  import {
    createTrackerLane,
    createTrackerTask,
    deleteTrackerLane,
    deleteTrackerTask,
    fetchTracker,
    moveTrackerLane,
    moveTrackerTask,
    updateTracker,
    updateTrackerLane,
    updateTrackerTask
  } from '$lib/task-tracker/client';
  import type {
    CreateTrackerTaskRequest,
    TrackerBoard,
    TrackerFilters,
    TrackerLane,
    TrackerPriority,
    TrackerTask,
    UpdateTrackerTaskRequest
  } from '$lib/task-tracker/types';
  import { GATEWAY_SESSION_RESET_EVENT, type Dispatch } from '$lib/gateway/client';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { onMount, tick } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import Icon from './Icon.svelte';
  import GuildMemberPicker from './GuildMemberPicker.svelte';
  import TaskTrackerSettingsDialog from './TaskTrackerSettingsDialog.svelte';
  import TaskTrackerTaskDialog from './TaskTrackerTaskDialog.svelte';

  let {
    channel,
    members = [],
    currentUser = null,
    memberRosterOpen = false,
    onOpenNavigation,
    onToggleMembers
  }: {
    channel: Channel;
    members?: GuildMemberSummary[];
    currentUser?: UserSummary | null;
    memberRosterOpen?: boolean;
    onOpenNavigation?: (button: HTMLButtonElement) => void;
    onToggleMembers?: () => void;
  } = $props();

  let board = $state<TrackerBoard | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let loadError = $state('');
  let actionError = $state('');
  let actionNotice = $state('');
  let actionBusy = $state(false);
  let editingTask = $state<TrackerTask | null>(null);
  let taskDialogOpen = $state(false);
  let taskDialogLane = $state<TrackerLane | null>(null);
  let taskCreateNonce = $state('');
  let settingsOpen = $state(false);
  let draggedTaskKey = $state('');
  let dropLaneKey = $state('');
  let dropIndex = $state(-1);
  let refreshTimer: number | null = null;
  let loadController: AbortController | null = null;
  let loadGeneration = 0;
  let trackerRoot = $state<HTMLElement | null>(null);
  let taskDialogTrigger = $state<HTMLElement | null>(null);
  let settingsDialogTrigger = $state<HTMLElement | null>(null);
  let pendingDialogFocus = $state<HTMLElement | null>(null);
  const collapsedLanes = new SvelteSet<string>();
  let filters = $state<TrackerFilters>({
    query: '',
    priority: 'all',
    assignee: '',
    hideCompleted: false
  });

  const lanes = $derived(orderedTrackerLanes(board));
  const visibleLanes = $derived(lanes.filter((lane) => !filters.hideCompleted || !lane.completed));
  const uniqueAssignees = $derived.by(() => {
    const users: Record<string, UserSummary> = {};
    for (const task of board?.tasks ?? []) {
      if (task.assignee) users[entityKey(task.assignee)] = task.assignee;
    }
    return Object.values(users).sort((left, right) =>
      userName(left).localeCompare(userName(right))
    );
  });
  const activeFilterCount = $derived(
    Number(Boolean(filters.query.trim())) +
      Number(filters.priority !== 'all') +
      Number(Boolean(filters.assignee)) +
      Number(filters.hideCompleted)
  );
  const visibleTaskCount = $derived(
    visibleLanes.reduce((count, lane) => count + filteredTasks(lane).length, 0)
  );
  const canCreate = $derived(trackerHasPermission(board, TrackerPermission.CREATE_TASKS));
  const canEditOwn = $derived(trackerHasPermission(board, TrackerPermission.EDIT_OWN_TASKS));
  const canManageTasks = $derived(trackerHasPermission(board, TrackerPermission.MANAGE_TASKS));
  const canAssign = $derived(trackerHasPermission(board, TrackerPermission.ASSIGN_TASKS));
  const canManageTracker = $derived(trackerHasPermission(board, TrackerPermission.MANAGE_TRACKER));

  $effect(() => {
    const target = pendingDialogFocus;
    if (!target || taskDialogOpen || settingsOpen || actionBusy) return;
    pendingDialogFocus = null;
    void tick().then(() => {
      const destination = target.isConnected && !target.matches(':disabled') ? target : trackerRoot;
      destination?.focus({ preventScroll: true });
    });
  });

  onMount(() => {
    void reload(true);
    const client = authenticatedGateway.client;
    const receive = (event: Event) => {
      const dispatch = (event as CustomEvent<Dispatch>).detail;
      if (trackerDispatchRequiresRefresh(dispatch, channel.id, channel.origin_domain)) {
        scheduleRefresh();
      }
    };
    const sessionReset = () => scheduleRefresh(0);
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') scheduleRefresh(0);
    };
    client.addEventListener('dispatch', receive);
    client.addEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
    window.addEventListener('focus', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      loadController?.abort();
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      client.removeEventListener('dispatch', receive);
      client.removeEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
      window.removeEventListener('focus', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  });

  function userName(user: UserSummary | null): string {
    return user?.display_name?.trim() || user?.username || 'Unassigned';
  }

  function userInitial(user: UserSummary): string {
    return userName(user).slice(0, 1).toLocaleUpperCase();
  }

  function canEditTask(task: TrackerTask): boolean {
    return canManageTasks || (canEditOwn && trackerTaskBelongsToUser(task, currentUser));
  }

  function canChangeTaskAssignee(task: TrackerTask): boolean {
    return trackerCanChangeAssignee(task, currentUser, canAssign);
  }

  function taskEditMode(task: TrackerTask) {
    return trackerTaskEditMode(canEditTask(task), canChangeTaskAssignee(task));
  }

  function isVersionConflict(caught: unknown): boolean {
    return (
      caught instanceof ApiError &&
      (caught.status === 412 || caught.code === 'TRACKER_VERSION_CONFLICT')
    );
  }

  async function actionFailure(caught: unknown, fallback: string): Promise<string> {
    if (!isVersionConflict(caught)) return userErrorMessage(caught, fallback);
    const conflictedTaskKey = editingTask?.key ?? '';
    const taskEditorWasOpen = taskDialogOpen;
    const settingsWereOpen = settingsOpen;
    if (taskEditorWasOpen) hideTaskDialog();
    if (settingsWereOpen) hideSettingsDialog();
    const refreshed = await reload(false);
    if (taskEditorWasOpen) {
      const subject = conflictedTaskKey ? `${conflictedTaskKey} changed` : 'This task changed';
      return refreshed
        ? `${subject} elsewhere. Your action was not applied, and the editor was closed so its older fields cannot overwrite the latest task. Reopen the task to review and reapply your edits.`
        : `${subject} elsewhere. Your action was not applied, and the editor was closed to protect the newer task. Kaede could not load the latest version; retry the board refresh before editing it again.`;
    }
    if (settingsWereOpen) {
      return refreshed
        ? 'This tracker changed elsewhere. Your action was not applied, and board settings were closed so older values cannot overwrite the latest version. Reopen settings to review your change.'
        : 'This tracker changed elsewhere. Your action was not applied, and board settings were closed. Kaede could not load the latest version; retry the board refresh before editing it again.';
    }
    return refreshed
      ? 'This tracker changed on another device. The latest version is loaded; review your change and try again.'
      : 'This tracker changed on another device, but Kaede could not load the latest version. Retry the board refresh before making another change.';
  }

  function filteredTasks(lane: TrackerLane): TrackerTask[] {
    return filterTrackerTasks(trackerTasksForLane(board, lane), filters);
  }

  function laneForTask(task: TrackerTask): TrackerLane | null {
    return (
      lanes.find((lane) => lane.id === task.lane_id && lane.origin_domain === task.lane_domain) ??
      null
    );
  }

  function scheduleRefresh(delay = 140) {
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      void reload(false);
    }, delay);
  }

  async function reload(initial: boolean): Promise<boolean> {
    const generation = ++loadGeneration;
    loadController?.abort();
    const controller = new AbortController();
    loadController = controller;
    if (initial || !board) loading = true;
    else refreshing = true;
    loadError = '';
    try {
      const loaded = await fetchTracker(channel, controller.signal);
      if (generation !== loadGeneration || controller.signal.aborted) return false;
      board = loaded;
      return true;
    } catch (caught) {
      if (controller.signal.aborted || generation !== loadGeneration) return false;
      loadError = userErrorMessage(caught, 'Could not load this task tracker. Try again.');
      // A transient refresh failure may retain the last projection with a
      // visible stale-state warning. Revoked/missing access must instead stop
      // rendering cached tracker contents immediately.
      if (caught instanceof ApiError && [401, 403, 404].includes(caught.status)) board = null;
      return false;
    } finally {
      if (generation === loadGeneration) {
        loading = false;
        refreshing = false;
      }
    }
  }

  function resetFilters() {
    filters = { query: '', priority: 'all', assignee: '', hideCompleted: false };
  }

  function toggleLane(lane: TrackerLane) {
    const key = entityKey(lane);
    if (collapsedLanes.has(key)) collapsedLanes.delete(key);
    else collapsedLanes.add(key);
  }

  function openCreateTask(lane: TrackerLane | null = null, trigger: HTMLElement | null = null) {
    const target = lane ?? lanes.find((candidate) => !candidate.completed) ?? lanes[0];
    if (!target || !canCreate) return;
    editingTask = null;
    taskDialogLane = target;
    taskCreateNonce = crypto.randomUUID();
    taskDialogTrigger = trigger;
    actionError = '';
    taskDialogOpen = true;
  }

  function openTask(task: TrackerTask, trigger: HTMLElement | null = null) {
    const target = laneForTask(task);
    if (!target) return;
    editingTask = task;
    taskDialogLane = target;
    taskCreateNonce = '';
    taskDialogTrigger = trigger;
    actionError = '';
    taskDialogOpen = true;
  }

  function hideTaskDialog() {
    pendingDialogFocus = taskDialogTrigger ?? trackerRoot;
    taskDialogOpen = false;
    editingTask = null;
    taskDialogLane = null;
    taskCreateNonce = '';
    taskDialogTrigger = null;
  }

  function closeTaskDialog() {
    if (actionBusy) return;
    hideTaskDialog();
    actionError = '';
  }

  function openSettings(trigger: HTMLElement | null = null) {
    settingsDialogTrigger = trigger;
    actionError = '';
    settingsOpen = true;
  }

  function hideSettingsDialog() {
    pendingDialogFocus = settingsDialogTrigger ?? trackerRoot;
    settingsOpen = false;
    settingsDialogTrigger = null;
  }

  function closeSettingsDialog() {
    if (actionBusy) return;
    hideSettingsDialog();
    actionError = '';
  }

  async function saveTask(
    request: CreateTrackerTaskRequest | UpdateTrackerTaskRequest,
    targetLane: TrackerLane
  ) {
    if (actionBusy) return;
    const target = editingTask;
    let detailsSavedBeforeMove = false;
    actionBusy = true;
    actionError = '';
    try {
      if (target) {
        let updated = await updateTrackerTask(channel, target, request as UpdateTrackerTaskRequest);
        if (updated.lane_id !== targetLane.id || updated.lane_domain !== targetLane.origin_domain) {
          detailsSavedBeforeMove = true;
          updated = await moveTrackerTask(
            channel,
            updated,
            targetLane,
            trackerTasksForLane(board, targetLane).length
          );
        }
      } else {
        await createTrackerTask(channel, {
          ...(request as CreateTrackerTaskRequest),
          client_nonce: taskCreateNonce || crypto.randomUUID()
        });
      }
      await reload(false);
      hideTaskDialog();
    } catch (caught) {
      if (detailsSavedBeforeMove && target) {
        hideTaskDialog();
        const refreshed = await reload(false);
        actionError = isVersionConflict(caught)
          ? refreshed
            ? `The changes to ${target.key} were saved, but its status changed elsewhere before it could be moved. The editor was closed and the latest task is loaded; reopen it to review and retry the move.`
            : `The changes to ${target.key} were saved, but its status changed elsewhere before it could be moved. The editor was closed, but Kaede could not reload the latest task; retry the board refresh.`
          : refreshed
            ? `${userErrorMessage(caught, 'Could not move the task to the selected status.')} The other task changes were saved. The editor was closed; reopen the latest task to retry the move.`
            : `${userErrorMessage(caught, 'Could not move the task to the selected status.')} The other task changes were saved, but Kaede could not reload the latest task.`;
      } else {
        actionError = await actionFailure(
          caught,
          'Could not save the task. Review it and try again.'
        );
      }
    } finally {
      actionBusy = false;
    }
  }

  async function removeTask() {
    if (!editingTask || actionBusy) return;
    actionBusy = true;
    actionError = '';
    try {
      await deleteTrackerTask(channel, editingTask);
      await reload(false);
      hideTaskDialog();
    } catch (caught) {
      actionError = await actionFailure(caught, 'Could not delete the task. Try again.');
    } finally {
      actionBusy = false;
    }
  }

  function dragStart(event: DragEvent, task: TrackerTask) {
    if (!canEditTask(task) || activeFilterCount > 0 || actionBusy) {
      event.preventDefault();
      return;
    }
    draggedTaskKey = entityKey(task);
    event.dataTransfer?.setData('application/x-kaede-tracker-task', draggedTaskKey);
    event.dataTransfer?.setData('text/plain', task.key);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function dragOver(event: DragEvent, lane: TrackerLane, index: number) {
    if (!draggedTaskKey || activeFilterCount > 0 || actionBusy) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    dropLaneKey = entityKey(lane);
    dropIndex = index;
  }

  function dragOverTask(event: DragEvent, lane: TrackerLane, index: number) {
    // Keep the lane-level dragover handler from replacing this precise task
    // boundary with the lane's catch-all "append to bottom" boundary.
    event.stopPropagation();
    const target = event.currentTarget as HTMLElement;
    const bounds = target.getBoundingClientRect();
    dragOver(event, lane, index + Number(event.clientY > bounds.top + bounds.height / 2));
  }

  function clearDrag() {
    draggedTaskKey = '';
    dropLaneKey = '';
    dropIndex = -1;
  }

  async function dropTask(event: DragEvent, lane: TrackerLane, index: number) {
    if (!draggedTaskKey || actionBusy) return;
    event.preventDefault();
    const key = draggedTaskKey;
    clearDrag();
    const task = board?.tasks.find((candidate) => entityKey(candidate) === key);
    if (!task) return;
    const position = trackerDropPosition(task, lane, trackerTasksForLane(board, lane), index);
    if (position === null) return;
    await moveTask(task, lane, position);
  }

  async function moveTask(task: TrackerTask, lane: TrackerLane, position: number) {
    if (!board || !canEditTask(task) || actionBusy) return;
    const previous = board;
    board = moveTaskInBoard(board, entityKey(task), lane, position);
    actionBusy = true;
    actionError = '';
    actionNotice = '';
    try {
      await moveTrackerTask(channel, task, lane, position);
      await reload(false);
      actionNotice = `${task.key} moved to ${lane.name}.`;
    } catch (caught) {
      if (!isVersionConflict(caught)) board = previous;
      actionError = await actionFailure(
        caught,
        'Could not move the task. The previous order was restored.'
      );
    } finally {
      actionBusy = false;
    }
  }

  async function savePrefix(prefix: string) {
    if (!board || actionBusy) return;
    actionBusy = true;
    actionError = '';
    try {
      board = await updateTracker(channel, board.version, { key_prefix: prefix });
    } catch (caught) {
      actionError = await actionFailure(caught, 'Could not update the task key prefix. Try again.');
    } finally {
      actionBusy = false;
    }
  }

  async function addLane(request: { name: string; color: number; completed?: boolean }) {
    if (actionBusy) return;
    actionBusy = true;
    actionError = '';
    try {
      await createTrackerLane(channel, request);
      await reload(false);
    } catch (caught) {
      actionError = await actionFailure(caught, 'Could not create the status. Try again.');
    } finally {
      actionBusy = false;
    }
  }

  async function saveLane(
    lane: TrackerLane,
    patch: { name?: string; color?: number; completed?: boolean }
  ) {
    if (actionBusy) return;
    actionBusy = true;
    actionError = '';
    try {
      await updateTrackerLane(channel, lane, patch);
      await reload(false);
    } catch (caught) {
      actionError = await actionFailure(caught, 'Could not update the status. Try again.');
    } finally {
      actionBusy = false;
    }
  }

  async function moveLane(lane: TrackerLane, position: number) {
    if (actionBusy) return;
    actionBusy = true;
    actionError = '';
    actionNotice = '';
    try {
      await moveTrackerLane(channel, lane, position);
      await reload(false);
      actionNotice = `${lane.name} moved to position ${position + 1}.`;
    } catch (caught) {
      actionError = await actionFailure(caught, 'Could not reorder the status. Try again.');
    } finally {
      actionBusy = false;
    }
  }

  async function removeLane(lane: TrackerLane) {
    if (actionBusy) return;
    actionBusy = true;
    actionError = '';
    try {
      await deleteTrackerLane(channel, lane);
      await reload(false);
    } catch (caught) {
      actionError = await actionFailure(
        caught,
        'Could not delete the status. Move its tasks elsewhere and try again.'
      );
    } finally {
      actionBusy = false;
    }
  }

  function priorityLabel(priority: TrackerPriority): string {
    if (priority === 'none') return 'None';
    if (priority === 'medium') return 'Mid';
    return `${priority.slice(0, 1).toLocaleUpperCase()}${priority.slice(1)}`;
  }

  function priorityDescription(priority: TrackerPriority): string {
    return priority === 'none' ? 'No priority' : `${priorityLabel(priority)} priority`;
  }

  function taskDomId(task: TrackerTask): string {
    const identity = `${task.id}-${task.origin_domain}`.replace(/[^a-zA-Z0-9_-]/g, '-');
    return `tracker-task-${identity}`;
  }

  function laneGlyph(lane: TrackerLane): string {
    if (lane.completed || lane.kind === 'completed') return '✓';
    if (lane.kind === 'planned') return '🚀';
    if (lane.kind === 'in_progress') return '◷';
    if (lane.kind === 'backlog') return '▣';
    return '○';
  }

  function duePresentation(
    task: TrackerTask
  ): { label: string; accessibleLabel: string; overdue: boolean } | null {
    if (!task.due_at) return null;
    const due = new Date(task.due_at);
    if (Number.isNaN(due.valueOf())) return null;
    return {
      label: new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(due),
      accessibleLabel: new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short'
      }).format(due),
      overdue: !task.completed_at && due.valueOf() < Date.now()
    };
  }
</script>

<section
  bind:this={trackerRoot}
  class="task-tracker"
  tabindex="-1"
  aria-labelledby="task-tracker-title"
>
  <span class="visually-hidden" aria-live="polite">{actionNotice}</span>
  <header class="tracker-heading">
    <div class="tracker-title-group">
      {#if onOpenNavigation}
        <button
          class="tracker-mobile-navigation"
          type="button"
          aria-label="Open guild navigation"
          aria-controls="guild-channel-navigation"
          onclick={(event) => onOpenNavigation?.(event.currentTarget)}
        >
          <span></span><span></span><span></span>
        </button>
      {/if}
      <div>
        <span class="tracker-eyebrow">Task tracker</span>
        <h1 id="task-tracker-title">{channel.name ?? 'Tasks'}</h1>
        {#if channel.topic}<p>{channel.topic}</p>{/if}
      </div>
    </div>
    <div class="tracker-heading-actions">
      {#if refreshing}<span class="refreshing-label" role="status">Refreshing…</span>{/if}
      {#if onToggleMembers}
        <button
          class:active={memberRosterOpen}
          class="settings-action member-action"
          type="button"
          aria-label={memberRosterOpen ? 'Hide member list' : 'Show member list'}
          aria-pressed={memberRosterOpen}
          title={memberRosterOpen ? 'Hide member list' : 'Show member list'}
          onclick={onToggleMembers}
        >
          <Icon name="users" size={19} />
        </button>
      {/if}
      {#if canManageTracker && board}
        <button
          class="settings-action"
          type="button"
          aria-label="Manage tracker statuses and task keys"
          title="Board settings"
          onclick={(event) => openSettings(event.currentTarget)}
        >
          <Icon name="settings" size={19} />
        </button>
      {/if}
      {#if canCreate}
        <button
          class="add-task"
          type="button"
          aria-label="Create task"
          title="Create task"
          disabled={!lanes.length || actionBusy}
          onclick={(event) => openCreateTask(null, event.currentTarget)}
        >
          <Icon name="plus" size={23} strokeWidth={2} />
        </button>
      {/if}
    </div>
  </header>

  <div class="tracker-toolbar" role="search">
    <label class="tracker-search">
      <Icon name="search" size={18} />
      <input bind:value={filters.query} aria-label="Search tasks" placeholder="Search tasks" />
    </label>
    <label>
      <span class="visually-hidden">Priority</span>
      <select bind:value={filters.priority} aria-label="Filter by priority">
        <option value="all">All priorities</option>
        <option value="urgent">Urgent</option>
        <option value="high">High</option>
        <option value="medium">Medium</option>
        <option value="low">Low</option>
        <option value="none">No priority</option>
      </select>
    </label>
    <label class="assignee-filter">
      <span class="visually-hidden">Assignee</span>
      <GuildMemberPicker
        fallbackUsers={uniqueAssignees}
        value={filters.assignee ? [filters.assignee] : []}
        optional
        placeholder="All assignees"
        onChange={(values) => (filters.assignee = values[0] ?? '')}
      />
    </label>
    <label class="completed-filter">
      <input bind:checked={filters.hideCompleted} type="checkbox" />
      Hide completed
    </label>
    {#if activeFilterCount}
      <button type="button" onclick={resetFilters}>Clear {activeFilterCount}</button>
    {/if}
  </div>

  {#if actionError && !taskDialogOpen && !settingsOpen}
    <div class="tracker-action-error" role="alert">
      <span>{actionError}</span>
      <button type="button" onclick={() => (actionError = '')}>Dismiss</button>
    </div>
  {/if}

  {#if loadError && board}
    <div class="tracker-refresh-error" role="alert">
      <Icon name="kanban" size={18} />
      <span
        ><strong>Board refresh failed.</strong>
        {loadError} The tasks below may be out of date.</span
      >
      <button type="button" disabled={refreshing} onclick={() => void reload(false)}>
        {refreshing ? 'Retrying…' : 'Retry'}
      </button>
    </div>
  {/if}

  {#if loading}
    <div class="tracker-loading" role="status" aria-label="Loading task tracker">
      {#each [0, 1, 2] as skeleton (skeleton)}
        <div><span></span><span></span><span></span></div>
      {/each}
    </div>
  {:else if loadError && !board}
    <div class="tracker-state tracker-state-error" role="alert">
      <Icon name="kanban" size={32} />
      <strong>Task tracker unavailable</strong>
      <p>{loadError}</p>
      <button type="button" onclick={() => void reload(true)}>Retry</button>
    </div>
  {:else if board && !lanes.length}
    <div class="tracker-state">
      <Icon name="kanban" size={32} />
      <strong>No statuses yet</strong>
      <p>
        {canManageTracker
          ? 'Create a status to start organizing work.'
          : 'A tracker manager needs to create the first status.'}
      </p>
      {#if canManageTracker}<button
          type="button"
          onclick={(event) => openSettings(event.currentTarget)}>Manage statuses</button
        >{/if}
    </div>
  {:else if board}
    <div class="tracker-lanes" aria-label="Task statuses">
      {#each visibleLanes as lane (entityKey(lane))}
        {@const tasks = filteredTasks(lane)}
        {@const collapsed = collapsedLanes.has(entityKey(lane))}
        <section
          class:drop-target={dropLaneKey === entityKey(lane)}
          class:completed-lane={lane.completed}
          class="tracker-lane"
          style:--lane-color={trackerColor(lane.color)}
          aria-labelledby={`tracker-lane-${lane.id}`}
          ondragover={(event) => dragOver(event, lane, tasks.length)}
          ondrop={(event) => void dropTask(event, lane, dropIndex < 0 ? tasks.length : dropIndex)}
        >
          <header class="lane-heading">
            <button
              class="lane-toggle"
              type="button"
              aria-expanded={!collapsed}
              aria-controls={`tracker-lane-tasks-${lane.id}`}
              onclick={() => toggleLane(lane)}
            >
              <Icon name={collapsed ? 'chevron-right' : 'chevron-down'} size={16} />
              <span class="lane-state-icon" aria-hidden="true">{laneGlyph(lane)}</span>
              <strong id={`tracker-lane-${lane.id}`}>{lane.name}</strong>
            </button>
            <div>
              <span class="lane-count" aria-label={`${lane.task_count} tasks`}
                >{lane.task_count}</span
              >
              {#if canCreate}
                <button
                  class="lane-add"
                  type="button"
                  aria-label={`Create a task in ${lane.name}`}
                  title={`Add task to ${lane.name}`}
                  onclick={(event) => openCreateTask(lane, event.currentTarget)}
                >
                  <Icon name="plus" size={17} />
                </button>
              {/if}
            </div>
          </header>

          {#if !collapsed}
            <div class="lane-tasks" id={`tracker-lane-tasks-${lane.id}`}>
              {#if dropLaneKey === entityKey(lane) && dropIndex === 0}
                <div class="task-drop-indicator" aria-hidden="true"></div>
              {/if}
              {#each tasks as task, index (entityKey(task))}
                {@const due = duePresentation(task)}
                {@const editable = canEditTask(task)}
                {@const movable = editable && activeFilterCount === 0}
                {@const taskId = taskDomId(task)}
                <article
                  class:dragging={draggedTaskKey === entityKey(task)}
                  class:overdue={due?.overdue}
                  class="tracker-task"
                  draggable={movable && !actionBusy}
                  ondragstart={(event) => dragStart(event, task)}
                  ondragend={clearDrag}
                  ondragover={(event) => dragOverTask(event, lane, index)}
                  ondrop={(event) => void dropTask(event, lane, dropIndex < 0 ? index : dropIndex)}
                >
                  {#if dropLaneKey === entityKey(lane) && dropIndex === index && index !== 0}
                    <div class="task-drop-indicator" aria-hidden="true"></div>
                  {/if}
                  <button
                    class="task-summary"
                    type="button"
                    aria-labelledby={`${taskId}-key ${taskId}-title`}
                    aria-describedby={`${taskId}-details`}
                    title={task.description || task.title}
                    onclick={(event) => openTask(task, event.currentTarget)}
                  >
                    <span
                      class:priority-none={task.priority === 'none'}
                      class={`task-priority priority-${task.priority}`}
                    >
                      {priorityLabel(task.priority)}
                    </span>
                    <code id={`${taskId}-key`}>{task.key}</code>
                    <span class="task-lane-mark" aria-hidden="true">{laneGlyph(lane)}</span>
                    <span class="task-title" id={`${taskId}-title`}>{task.title}</span>
                    {#if due}
                      <time class:overdue={due.overdue} datetime={task.due_at ?? undefined}>
                        <Icon name="clock" size={13} />{due.label}
                      </time>
                    {/if}
                    <span
                      class:unassigned={!task.assignee}
                      class="task-assignee"
                      aria-hidden="true"
                      title={task.assignee
                        ? `Assigned to ${userName(task.assignee)}`
                        : 'Unassigned'}
                    >
                      {#if task.assignee?.avatar_hash}
                        <img
                          src={assetUrl(task.assignee.avatar_hash, 'thumbnail_128', task.assignee)}
                          alt=""
                        />
                      {:else if task.assignee}
                        {userInitial(task.assignee)}
                      {:else}
                        <Icon name="user" size={15} />
                      {/if}
                    </span>
                    <span class="visually-hidden" id={`${taskId}-details`}>
                      Open task. {priorityDescription(task.priority)}. Status {lane.name}.
                      {#if due}
                        Due {due.accessibleLabel}{due.overdue ? ', overdue' : ''}.
                      {:else}
                        No due date.
                      {/if}
                      {task.assignee ? `Assigned to ${userName(task.assignee)}.` : 'Unassigned.'}
                    </span>
                  </button>
                  {#if movable}
                    <div class="task-move-controls">
                      <button
                        type="button"
                        aria-label={`Move ${task.key} up`}
                        title="Move up"
                        disabled={actionBusy || index === 0}
                        onclick={() => void moveTask(task, lane, index - 1)}>↑</button
                      >
                      <button
                        type="button"
                        aria-label={`Move ${task.key} down`}
                        title="Move down"
                        disabled={actionBusy || index === tasks.length - 1}
                        onclick={() => void moveTask(task, lane, index + 1)}>↓</button
                      >
                      <label>
                        <span class="visually-hidden">Move {task.key} to another status</span>
                        <select
                          aria-label={`Move ${task.key} to status`}
                          value={entityKey(lane)}
                          disabled={actionBusy}
                          onchange={(event) => {
                            const target = lanes.find(
                              (candidate) => entityKey(candidate) === event.currentTarget.value
                            );
                            if (target && entityKey(target) !== entityKey(lane)) {
                              void moveTask(
                                task,
                                target,
                                trackerTasksForLane(board, target).length
                              );
                            }
                          }}
                        >
                          {#each lanes as targetLane (entityKey(targetLane))}
                            <option value={entityKey(targetLane)}>{targetLane.name}</option>
                          {/each}
                        </select>
                      </label>
                    </div>
                  {/if}
                </article>
                {#if dropLaneKey === entityKey(lane) && dropIndex === index + 1}
                  <div class="task-drop-indicator" aria-hidden="true"></div>
                {/if}
              {:else}
                <div class="lane-empty">
                  {activeFilterCount
                    ? 'No matching tasks in this status.'
                    : 'No tasks in this status.'}
                </div>
              {/each}
            </div>
          {/if}
        </section>
      {/each}
    </div>

    {#if activeFilterCount && visibleTaskCount === 0}
      <div class="tracker-state compact-state">
        <Icon name="search" size={27} />
        <strong>No tasks match these filters</strong>
        <button type="button" onclick={resetFilters}>Clear filters</button>
      </div>
    {/if}
  {/if}
</section>

{#if taskDialogOpen && taskDialogLane}
  {#key editingTask ? `${entityKey(editingTask)}:${editingTask.version}` : `new:${taskCreateNonce}`}
    <TaskTrackerTaskDialog
      task={editingTask}
      initialLane={taskDialogLane}
      {lanes}
      {members}
      guildRef={channel.guild_id && channel.guild_domain
        ? entityRef({ id: channel.guild_id, origin_domain: channel.guild_domain })
        : null}
      {canAssign}
      {currentUser}
      canDelete={Boolean(editingTask && canEditTask(editingTask))}
      assignmentOnly={Boolean(editingTask && taskEditMode(editingTask) === 'assignment')}
      readOnly={Boolean(editingTask && taskEditMode(editingTask) === 'read-only')}
      busy={actionBusy}
      error={actionError}
      onSave={saveTask}
      onDelete={removeTask}
      onClose={closeTaskDialog}
    />
  {/key}
{/if}

{#if settingsOpen && board}
  <TaskTrackerSettingsDialog
    {board}
    {lanes}
    busy={actionBusy}
    error={actionError}
    onPrefix={savePrefix}
    onCreateLane={addLane}
    onUpdateLane={saveLane}
    onMoveLane={moveLane}
    onDeleteLane={removeLane}
    onClose={closeSettingsDialog}
  />
{/if}

<style>
  .task-tracker {
    min-width: 0;
    min-height: 0;
    overflow: auto;
    padding: clamp(1rem, 2.3vw, 2rem);
    background:
      radial-gradient(
        circle at 88% -20%,
        color-mix(in srgb, var(--accent) 8%, transparent),
        transparent 36rem
      ),
      var(--surface);
  }

  .tracker-heading,
  .tracker-title-group,
  .tracker-heading-actions,
  .tracker-toolbar,
  .lane-heading,
  .lane-heading > div,
  .lane-toggle,
  .task-summary,
  .task-move-controls,
  .tracker-action-error,
  .tracker-refresh-error {
    display: flex;
    min-width: 0;
    align-items: center;
  }

  .tracker-heading {
    justify-content: space-between;
    gap: 1rem;
    max-width: 1180px;
    margin: 0 auto 1rem;
  }

  .tracker-title-group {
    gap: 0.7rem;
  }

  .tracker-title-group > div {
    min-width: 0;
  }

  .tracker-mobile-navigation {
    display: none;
    width: 42px;
    height: 42px;
    flex: 0 0 auto;
    place-items: center;
    gap: 4px;
    border: 1px solid var(--line);
    border-radius: 11px;
    padding: 10px;
    color: var(--text-soft);
    background: var(--surface-subtle);
    cursor: pointer;
  }

  .tracker-mobile-navigation span {
    display: block;
    width: 19px;
    height: 2px;
    border-radius: 99px;
    background: currentColor;
  }

  .tracker-eyebrow {
    display: block;
    margin-bottom: 0.25rem;
    color: var(--text-muted);
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(1.45rem, 2.5vw, 2rem);
    letter-spacing: -0.035em;
  }

  .tracker-heading p {
    max-width: 720px;
    margin: 0.25rem 0 0;
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.76rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tracker-heading-actions {
    flex: 0 0 auto;
    gap: 0.45rem;
  }

  .refreshing-label {
    color: var(--text-muted);
    font-size: 0.67rem;
  }

  .settings-action,
  .add-task,
  .lane-add,
  .tracker-toolbar button,
  .tracker-state button,
  .tracker-action-error button,
  .tracker-refresh-error button,
  .task-move-controls button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 0;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font: inherit;
    font-weight: 730;
    cursor: pointer;
  }

  .settings-action,
  .add-task {
    width: 44px;
    height: 44px;
    border: 1px solid var(--line);
    border-radius: 14px;
  }

  .add-task {
    border-color: var(--accent);
    color: var(--on-accent);
    background: var(--accent);
    box-shadow: 0 7px 20px color-mix(in srgb, var(--accent) 23%, transparent);
  }

  .tracker-toolbar {
    position: sticky;
    z-index: 5;
    top: -1rem;
    max-width: 1180px;
    flex-wrap: wrap;
    gap: 0.55rem;
    margin: 0 auto 1rem;
    padding: 0.7rem;
    border: 1px solid var(--line-soft);
    border-radius: 13px;
    background: color-mix(in srgb, var(--surface) 94%, transparent);
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(14px);
  }

  .tracker-search {
    display: flex;
    min-width: min(260px, 100%);
    flex: 1 1 280px;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-muted);
  }

  .tracker-toolbar input:not([type='checkbox']),
  .tracker-toolbar select,
  .task-move-controls select {
    min-height: 38px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0 0.65rem;
    color: var(--text);
    background: var(--surface-subtle);
    font: inherit;
    font-size: 0.73rem;
  }

  .task-move-controls option {
    color: var(--text);
    background: var(--surface-raised);
  }

  .tracker-search input {
    width: 100%;
  }

  .completed-filter {
    display: flex;
    min-height: 38px;
    align-items: center;
    gap: 0.45rem;
    padding: 0 0.35rem;
    color: var(--text-muted);
    font-size: 0.7rem;
    font-weight: 680;
    white-space: nowrap;
  }

  .tracker-toolbar button,
  .tracker-state button,
  .tracker-action-error button,
  .tracker-refresh-error button {
    min-height: 38px;
    border-radius: 9px;
    padding: 0 0.75rem;
    font-size: 0.7rem;
  }

  .tracker-action-error {
    max-width: 1180px;
    justify-content: space-between;
    gap: 0.75rem;
    margin: 0 auto 0.8rem;
    border: 1px solid color-mix(in srgb, var(--danger) 42%, var(--line));
    border-radius: 10px;
    padding: 0.6rem 0.7rem 0.6rem 0.85rem;
    color: var(--danger);
    background: var(--danger-soft);
    font-size: 0.73rem;
  }

  .tracker-refresh-error {
    max-width: 1180px;
    justify-content: flex-start;
    gap: 0.65rem;
    margin: 0 auto 0.8rem;
    border: 1px solid color-mix(in srgb, var(--warning) 38%, var(--line));
    border-radius: 10px;
    padding: 0.6rem 0.7rem 0.6rem 0.85rem;
    color: var(--text-soft);
    background: var(--warning-soft);
    font-size: 0.73rem;
  }

  .tracker-refresh-error span {
    min-width: 0;
    flex: 1;
  }

  .tracker-refresh-error strong {
    color: var(--text);
  }

  .tracker-lanes {
    display: grid;
    max-width: 1180px;
    gap: 0.9rem;
    margin: 0 auto;
  }

  .tracker-lane {
    position: relative;
    min-width: 0;
    border: 1px solid var(--line-soft);
    border-radius: 13px;
    background: color-mix(in srgb, var(--surface-subtle) 74%, transparent);
    box-shadow: var(--shadow-sm);
    transition:
      border-color 120ms ease,
      box-shadow 120ms ease;
  }

  .tracker-lane::before {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    width: 4px;
    border-radius: 13px 0 0 13px;
    background: var(--lane-color);
    content: '';
  }

  .tracker-lane.drop-target {
    border-color: var(--lane-color);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--lane-color) 20%, transparent);
  }

  .lane-heading {
    min-height: 54px;
    justify-content: space-between;
    gap: 0.7rem;
    padding: 0.45rem 0.65rem 0.45rem 0.85rem;
  }

  .lane-toggle {
    min-height: 42px;
    flex: 1;
    gap: 0.65rem;
    border: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .lane-state-icon {
    display: grid;
    width: 24px;
    height: 24px;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid color-mix(in srgb, var(--lane-color) 72%, var(--line));
    border-radius: 999px;
    color: var(--lane-color);
    font-size: 0.8rem;
    font-weight: 900;
  }

  .lane-toggle strong {
    overflow: hidden;
    font-size: 0.98rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .lane-heading > div {
    gap: 0.35rem;
  }

  .lane-count {
    display: grid;
    min-width: 28px;
    height: 26px;
    place-items: center;
    border-radius: 7px;
    color: var(--text-muted);
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--line-soft);
    font-family: var(--font-mono);
    font-size: 0.67rem;
    font-weight: 800;
  }

  .lane-add {
    width: 34px;
    height: 34px;
    border-radius: 8px;
    background: transparent;
  }

  .lane-add:hover,
  .settings-action:hover,
  .task-move-controls button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }

  .member-action.active {
    color: var(--accent);
    background: var(--accent-soft);
  }

  .lane-tasks {
    display: grid;
    gap: 2px;
    border-top: 1px solid var(--line-soft);
    padding: 0.35rem 0.55rem 0.55rem 0.75rem;
  }

  .tracker-task {
    position: relative;
    display: grid;
    min-width: 0;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid transparent;
    border-radius: 9px;
    transition:
      border-color 120ms ease,
      background-color 120ms ease,
      opacity 120ms ease;
  }

  .tracker-task:hover,
  .tracker-task:focus-within {
    border-color: var(--line-soft);
    background: var(--surface-raised);
  }

  .tracker-task.dragging {
    opacity: 0.42;
  }

  .task-summary {
    display: grid;
    min-height: 45px;
    grid-template-columns: 58px 76px 20px minmax(150px, 1fr) auto 32px;
    gap: 0.45rem;
    border: 0;
    padding: 0.35rem 0.4rem;
    color: var(--text);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .task-priority {
    display: inline-grid;
    min-width: 42px;
    min-height: 23px;
    place-items: center;
    justify-self: start;
    border-radius: 6px;
    padding: 0 0.45rem;
    font-size: 0.62rem;
    font-weight: 800;
  }

  .priority-none {
    color: var(--text-muted);
    background: var(--surface-hover);
  }

  .priority-low {
    color: color-mix(in srgb, #22c55e 78%, var(--text));
    background: color-mix(in srgb, #22c55e 14%, transparent);
  }

  .priority-medium {
    color: color-mix(in srgb, #f59e0b 82%, var(--text));
    background: color-mix(in srgb, #f59e0b 14%, transparent);
  }

  .priority-high,
  .priority-urgent {
    color: color-mix(in srgb, #ef4444 82%, var(--text));
    background: color-mix(in srgb, #ef4444 14%, transparent);
  }

  .priority-urgent {
    box-shadow: inset 0 0 0 1px color-mix(in srgb, #ef4444 35%, transparent);
  }

  .task-summary code {
    overflow: hidden;
    color: var(--text-muted);
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-lane-mark {
    color: var(--lane-color);
    font-size: 0.86rem;
    font-weight: 900;
  }

  .task-title {
    overflow: hidden;
    font-size: 0.8rem;
    font-weight: 580;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-summary time {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--text-muted);
    font-size: 0.62rem;
    white-space: nowrap;
  }

  .task-summary time.overdue {
    color: var(--danger);
  }

  .task-assignee {
    display: grid;
    width: 28px;
    height: 28px;
    place-items: center;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 9px;
    color: var(--text-soft);
    background: var(--surface-hover);
    font-size: 0.64rem;
    font-weight: 820;
  }

  .task-assignee img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .task-assignee.unassigned {
    color: var(--text-muted);
    opacity: 0.68;
  }

  .task-move-controls {
    max-width: 0;
    gap: 0.2rem;
    overflow: hidden;
    opacity: 0;
    transition:
      max-width 140ms ease,
      opacity 100ms ease;
  }

  .tracker-task:hover .task-move-controls,
  .tracker-task:focus-within .task-move-controls {
    max-width: 230px;
    opacity: 1;
  }

  .task-move-controls button {
    width: 30px;
    height: 30px;
    border-radius: 7px;
    padding: 0;
    background: transparent;
  }

  .task-move-controls select {
    width: 112px;
    min-height: 30px;
    padding-inline: 0.4rem;
    font-size: 0.64rem;
  }

  .task-drop-indicator {
    height: 2px;
    margin: 0 0.4rem;
    border-radius: 999px;
    background: var(--lane-color);
    box-shadow: 0 0 8px color-mix(in srgb, var(--lane-color) 45%, transparent);
  }

  .lane-empty {
    min-height: 50px;
    padding: 1rem;
    color: var(--text-muted);
    font-size: 0.7rem;
    text-align: center;
  }

  .tracker-state,
  .tracker-loading {
    max-width: 1180px;
    margin: 0 auto;
  }

  .tracker-state {
    display: grid;
    min-height: 260px;
    place-content: center;
    justify-items: center;
    gap: 0.5rem;
    border: 1px solid var(--line-soft);
    border-radius: 14px;
    padding: 1rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
    text-align: center;
  }

  .tracker-state strong {
    color: var(--text);
  }

  .tracker-state p {
    max-width: 440px;
    margin: 0;
    font-size: 0.75rem;
  }

  .tracker-state-error {
    border-color: color-mix(in srgb, var(--danger) 42%, var(--line));
  }

  .compact-state {
    min-height: 150px;
    margin-top: 0.8rem;
  }

  .tracker-loading {
    display: grid;
    gap: 0.8rem;
  }

  .tracker-loading > div {
    display: grid;
    height: 110px;
    gap: 0.45rem;
    border: 1px solid var(--line-soft);
    border-radius: 13px;
    padding: 0.8rem;
    background: var(--surface-subtle);
  }

  .tracker-loading span {
    display: block;
    width: 35%;
    height: 14px;
    border-radius: 999px;
    background: var(--surface-hover);
    animation: tracker-pulse 1.2s ease-in-out infinite alternate;
  }

  .tracker-loading span:nth-child(2) {
    width: 72%;
  }

  .tracker-loading span:nth-child(3) {
    width: 56%;
  }

  @keyframes tracker-pulse {
    to {
      opacity: 0.42;
    }
  }

  button:disabled,
  select:disabled {
    cursor: not-allowed;
    opacity: 0.52;
  }

  @media (max-width: 900px) {
    .task-summary {
      grid-template-columns: 54px 68px 18px minmax(120px, 1fr) auto 30px;
    }

    .task-move-controls {
      max-width: 34px;
      opacity: 1;
    }

    .task-move-controls > button {
      display: none;
    }

    .task-move-controls select {
      width: 34px;
      color: transparent;
      background-image: none;
    }
  }

  @media (max-width: 620px) {
    .task-tracker {
      padding: 0.85rem 0.65rem calc(1rem + env(safe-area-inset-bottom));
    }

    .tracker-heading {
      align-items: flex-start;
    }

    .tracker-mobile-navigation {
      display: grid;
    }

    .tracker-heading p,
    .refreshing-label {
      display: none;
    }

    .settings-action,
    .add-task {
      width: 42px;
      height: 42px;
    }

    .tracker-toolbar {
      top: -0.85rem;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      padding: 0.55rem;
    }

    .tracker-search {
      min-width: 0;
      grid-column: 1 / -1;
    }

    .tracker-toolbar label:not(.tracker-search):not(.completed-filter) select {
      width: 100%;
    }

    .completed-filter {
      justify-content: center;
    }

    .task-summary {
      min-height: 58px;
      grid-template-columns: 48px 62px minmax(100px, 1fr) 30px;
      grid-template-rows: auto auto;
      gap: 0.2rem 0.4rem;
    }

    .task-priority {
      grid-column: 1;
      grid-row: 1;
    }

    .task-summary code {
      grid-column: 2;
      grid-row: 1;
    }

    .task-lane-mark {
      display: none;
    }

    .task-title {
      grid-column: 1 / 4;
      grid-row: 2;
    }

    .task-summary time {
      grid-column: 3;
      grid-row: 1;
      justify-self: end;
    }

    .task-assignee {
      grid-column: 4;
      grid-row: 1 / 3;
    }

    .task-move-controls {
      max-width: 32px;
    }

    .lane-heading {
      padding-left: 0.7rem;
    }
  }

  @media (hover: none), (pointer: coarse) {
    .tracker-task {
      grid-template-columns: minmax(0, 1fr) 34px;
    }

    .task-move-controls {
      max-width: 34px;
      opacity: 1;
    }

    .task-move-controls > button {
      display: none;
    }

    .task-move-controls select {
      width: 34px;
      color: transparent;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .tracker-loading span {
      animation: none;
    }
  }
</style>
