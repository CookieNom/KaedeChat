<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import type { Channel, Role } from '$lib/chat/types';
  import GuildMemberPicker from './GuildMemberPicker.svelte';

  interface PermissionEntry {
    id: string;
    type: 'role' | 'user' | 'channel';
    permission: boolean;
  }

  interface PermissionScope {
    id: string;
    application_ref: string;
    application_name: string;
    guild_ref: string;
    command: { ref: string; name: string; type: string } | null;
    command_ref: string | null;
    synced: boolean;
    permissions: PermissionEntry[];
  }

  let {
    guildRef,
    applicationRef,
    roles = [],
    channels = [],
    canManage
  }: {
    guildRef: string;
    applicationRef: string;
    roles?: Role[];
    channels?: Channel[];
    canManage: boolean;
  } = $props();

  let open = $state(false);
  let loading = $state(false);
  let saving = $state(false);
  let loadedFor = $state('');
  let error = $state('');
  let notice = $state('');
  let scopes = $state<PermissionScope[]>([]);
  let selectedId = $state('');
  let draft = $state<PermissionEntry[]>([]);
  let targetType = $state<PermissionEntry['type']>('role');
  let targetId = $state('');
  let targetPermission = $state(true);

  const selected = $derived(scopes.find((scope) => scope.id === selectedId) ?? null);
  const applicationDefaults = $derived(scopes.find((scope) => scope.command === null) ?? null);
  const roleOptions = $derived(
    roles.map((role) => ({
      value: `${role.id}@${role.origin_domain}`,
      label: role.name
    }))
  );
  const channelOptions = $derived([
    { value: allChannelsRef(guildRef), label: 'All channels' },
    ...channels
      .filter((channel) => ![4, 10, 11, 12].includes(channel.type))
      .map((channel) => ({
        value: `${channel.id}@${channel.origin_domain}`,
        label: `#${channel.name ?? 'channel'}`
      }))
  ]);

  async function load() {
    const key = `${guildRef}\n${applicationRef}`;
    if (loading || loadedFor === key) return;
    loading = true;
    error = '';
    try {
      scopes = await api<PermissionScope[]>(
        `/applications/${encodeURIComponent(applicationRef)}/guilds/${encodeURIComponent(guildRef)}/commands/permissions`
      );
      loadedFor = key;
      selectScope(scopes[0]?.id ?? '');
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load command permissions.');
    } finally {
      loading = false;
    }
  }

  function selectScope(id: string) {
    selectedId = id;
    const scope = scopes.find((item) => item.id === id);
    draft = scope ? scope.permissions.map((entry) => ({ ...entry })) : [];
    error = '';
    notice = '';
  }

  function addEntry() {
    if (!targetId || draft.length >= 100) return;
    if (draft.some((entry) => entry.type === targetType && entry.id === targetId)) {
      error = 'That role, member, or channel already has an override.';
      return;
    }
    draft = [...draft, { id: targetId, type: targetType, permission: targetPermission }];
    targetId = '';
    error = '';
  }

  function syncWithApplication() {
    if (!selected?.command || !applicationDefaults) return;
    draft = applicationDefaults.permissions.map((entry) => ({ ...entry }));
    notice = 'Application defaults copied. Save to synchronize this command.';
  }

  async function save() {
    if (!selected || !canManage || saving) return;
    saving = true;
    error = '';
    notice = '';
    try {
      const saved = await api<PermissionScope>(
        `/applications/${encodeURIComponent(applicationRef)}/guilds/${encodeURIComponent(guildRef)}/commands/${encodeURIComponent(selected.id)}/permissions`,
        {
          method: 'PUT',
          body: JSON.stringify({ permissions: draft })
        }
      );
      scopes = scopes.map((scope) => (scope.id === saved.id ? saved : scope));
      selectScope(saved.id);
      notice = saved.synced ? 'This command now uses the app defaults.' : 'Command access updated.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update command permissions.');
    } finally {
      saving = false;
    }
  }

  function targetLabel(entry: PermissionEntry): string {
    if (entry.type === 'role') {
      return roleOptions.find((option) => option.value === entry.id)?.label ?? entry.id;
    }
    if (entry.type === 'channel') {
      return channelOptions.find((option) => option.value === entry.id)?.label ?? entry.id;
    }
    return entry.id;
  }

  function allChannelsRef(ref: string): string {
    const [id, domain = ''] = ref.split('@', 2);
    try {
      return `${BigInt(id) - 1n}@${domain}`;
    } catch {
      return '';
    }
  }
</script>

<details
  class="command-permissions"
  bind:open
  ontoggle={() => {
    if (open) void load();
  }}
>
  <summary>Command permissions</summary>
  {#if loading}
    <p role="status">Loading command permissions…</p>
  {:else if error && scopes.length === 0}
    <p class="error" role="alert">{error}</p>
    <button type="button" onclick={() => void load()}>Retry</button>
  {:else if scopes.length === 0}
    <p>This app has no guild commands to configure.</p>
  {:else}
    <label>
      Command
      <select value={selectedId} onchange={(event) => selectScope(event.currentTarget.value)}>
        {#each scopes as scope (scope.id)}
          <option value={scope.id}>
            {scope.command ? `/${scope.command.name}` : `All ${scope.application_name} commands`}
          </option>
        {/each}
      </select>
    </label>
    {#if selected}
      <p class="scope-note">
        {selected.command
          ? selected.synced
            ? 'Synced with this app’s default command access.'
            : 'This command has custom access.'
          : 'Default access inherited by commands without custom overrides.'}
      </p>
      {#if draft.length === 0}
        <p>No role, member, or channel overrides.</p>
      {:else}
        <ul>
          {#each draft as entry, index (`${entry.type}:${entry.id}`)}
            <li>
              <span><strong>{entry.type}</strong> · {targetLabel(entry)}</span>
              <button
                type="button"
                class:denied={!entry.permission}
                disabled={!canManage || saving}
                onclick={() =>
                  (draft = draft.map((item, itemIndex) =>
                    itemIndex === index ? { ...item, permission: !item.permission } : item
                  ))}>{entry.permission ? 'Allowed' : 'Denied'}</button
              >
              <button
                type="button"
                aria-label={`Remove ${targetLabel(entry)} override`}
                disabled={!canManage || saving}
                onclick={() => (draft = draft.filter((_, itemIndex) => itemIndex !== index))}
                >Remove</button
              >
            </li>
          {/each}
        </ul>
      {/if}
      {#if canManage}
        <div class="add-entry">
          <select
            aria-label="Permission target type"
            bind:value={targetType}
            onchange={() => (targetId = '')}
          >
            <option value="role">Role</option>
            <option value="user">Member</option>
            <option value="channel">Channel</option>
          </select>
          {#if targetType === 'user'}
            <GuildMemberPicker
              {guildRef}
              value={targetId ? [targetId] : []}
              optional
              placeholder="Choose a member"
              disabled={saving}
              onChange={(values) => (targetId = values[0] ?? '')}
            />
          {:else}
            <select aria-label={`Choose a ${targetType}`} bind:value={targetId} disabled={saving}>
              <option value="">Choose a {targetType}</option>
              {#each targetType === 'role' ? roleOptions : channelOptions as option (option.value)}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          {/if}
          <select aria-label="Allow or deny" bind:value={targetPermission} disabled={saving}>
            <option value={true}>Allow</option>
            <option value={false}>Deny</option>
          </select>
          <button
            type="button"
            disabled={!targetId || draft.length >= 100 || saving}
            onclick={addEntry}>Add</button
          >
        </div>
        <div class="actions">
          {#if selected.command}
            <button type="button" disabled={saving} onclick={syncWithApplication}
              >Use app defaults</button
            >
          {/if}
          <button type="button" disabled={saving} onclick={() => void save()}
            >{saving ? 'Saving…' : 'Save permissions'}</button
          >
        </div>
      {:else}
        <p>Manage Server and Manage Roles are required to change command access.</p>
      {/if}
      {#if error}<p class="error" role="alert">{error}</p>{/if}
      {#if notice}<p class="notice">{notice}</p>{/if}
    {/if}
  {/if}
</details>

<style>
  .command-permissions {
    margin-top: 0.7rem;
  }
  summary {
    cursor: pointer;
    font-weight: 750;
  }
  label,
  .add-entry,
  .actions,
  li {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    flex-wrap: wrap;
  }
  label {
    margin-top: 0.8rem;
    font-weight: 700;
  }
  select,
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.5rem 0.65rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
  }
  ul {
    display: grid;
    gap: 0.4rem;
    padding: 0;
    list-style: none;
  }
  li span {
    min-width: 12rem;
    flex: 1;
    overflow-wrap: anywhere;
  }
  li button.denied {
    color: var(--danger, #ef6767);
  }
  .scope-note,
  .command-permissions p {
    color: var(--text-muted);
  }
  .add-entry {
    margin-top: 0.8rem;
  }
  .add-entry :global(.guild-member-picker) {
    min-width: min(20rem, 100%);
    flex: 1;
  }
  .actions {
    justify-content: flex-end;
    margin-top: 0.8rem;
  }
  .actions button:last-child {
    color: white;
    background: var(--accent);
  }
  .error {
    color: var(--danger, #ef6767) !important;
  }
  .notice {
    color: var(--accent) !important;
  }
</style>
