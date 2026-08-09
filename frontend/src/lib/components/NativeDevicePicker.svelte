<script lang="ts">
  import Icon, { type IconName } from '$lib/components/Icon.svelte';
  import { onMount } from 'svelte';

  interface DeviceOption {
    id: string;
    label: string;
    is_default?: boolean;
    channels?: number;
    sample_rate?: number;
  }

  let {
    label,
    description = '',
    icon,
    selectedId = '',
    defaultLabel = 'System default',
    options = [],
    onSelect
  }: {
    label: string;
    description?: string;
    icon: IconName;
    selectedId?: string;
    defaultLabel?: string;
    options?: DeviceOption[];
    onSelect: (id: string) => void;
  } = $props();

  let root: HTMLElement;
  let searchInput = $state<HTMLInputElement>();
  let open = $state(false);
  let query = $state('');
  const selected = $derived(options.find((option) => option.id === selectedId));
  const unavailable = $derived(Boolean(selectedId && !selected));
  const filtered = $derived(
    options.filter((option) => option.label.toLowerCase().includes(query.trim().toLowerCase()))
  );

  onMount(() => {
    const dismiss = (event: PointerEvent) => {
      if (open && !root?.contains(event.target as Node)) close();
    };
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && open) {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener('pointerdown', dismiss, true);
    window.addEventListener('keydown', onKeydown);
    return () => {
      window.removeEventListener('pointerdown', dismiss, true);
      window.removeEventListener('keydown', onKeydown);
    };
  });

  function toggle() {
    open = !open;
    query = '';
    if (open) requestAnimationFrame(() => searchInput?.focus());
  }

  function close() {
    open = false;
    query = '';
  }

  function choose(id: string) {
    onSelect(id);
    close();
  }

  function deviceDetail(option: DeviceOption): string {
    const details: string[] = [];
    if (option.sample_rate) details.push(`${Math.round(option.sample_rate / 1000)} kHz`);
    if (option.channels) {
      details.push(
        option.channels === 1
          ? 'Mono'
          : option.channels === 2
            ? 'Stereo'
            : `${option.channels} channels`
      );
    }
    return details.join(' · ');
  }
</script>

<div class="device-picker" bind:this={root}>
  <span class="device-picker-label">{label}</span>
  {#if description}<span class="device-picker-description">{description}</span>{/if}
  <button
    class:open
    class:unavailable
    class="device-picker-trigger"
    type="button"
    aria-haspopup="listbox"
    aria-expanded={open}
    onclick={toggle}
  >
    <span class="device-picker-icon"><Icon name={icon} size={20} /></span>
    <span class="device-picker-value">
      <strong>{selected?.label ?? (unavailable ? 'Device unavailable' : defaultLabel)}</strong>
      <small>
        {#if unavailable}
          Using the system default until this device returns
        {:else if selected}
          {deviceDetail(selected) ||
            (selected.is_default ? 'Current system default' : 'Selected device')}
        {:else}
          Automatically follows your operating system
        {/if}
      </small>
    </span>
    <Icon name="chevron-down" size={18} />
  </button>

  {#if open}
    <div class="device-picker-popover">
      {#if options.length > 5}
        <label class="device-picker-search">
          <Icon name="search" size={17} />
          <input bind:this={searchInput} bind:value={query} placeholder="Search devices" />
        </label>
      {/if}
      <div class="device-picker-options" role="listbox" aria-label={label}>
        <button
          class:selected={!selectedId}
          type="button"
          role="option"
          aria-selected={!selectedId}
          onclick={() => choose('')}
        >
          <span class="device-option-icon"><Icon name={icon} size={18} /></span>
          <span><strong>{defaultLabel}</strong><small>Recommended</small></span>
          {#if !selectedId}<Icon name="check" size={18} />{/if}
        </button>
        {#each filtered as option (option.id)}
          <button
            class:selected={option.id === selectedId}
            type="button"
            role="option"
            aria-selected={option.id === selectedId}
            onclick={() => choose(option.id)}
          >
            <span class="device-option-icon"><Icon name={icon} size={18} /></span>
            <span>
              <strong>{option.label}</strong>
              <small
                >{deviceDetail(option) ||
                  (option.is_default ? 'System default' : 'Available')}</small
              >
            </span>
            {#if option.id === selectedId}<Icon name="check" size={18} />{/if}
          </button>
        {/each}
        {#if filtered.length === 0}
          <p>No matching devices.</p>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .device-picker {
    position: relative;
    min-width: 0;
  }

  .device-picker-label {
    display: block;
    margin-bottom: 4px;
    color: var(--text);
    font-size: 0.82rem;
    font-weight: 800;
  }

  .device-picker-description {
    display: block;
    margin: -1px 0 8px;
    color: var(--text-muted);
    font-size: 0.7rem;
  }

  .device-picker-trigger {
    display: grid;
    width: 100%;
    min-height: 68px;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    border: 1px solid var(--line-strong, var(--line));
    border-radius: 14px;
    padding: 10px 13px;
    color: var(--text-soft);
    background: var(--surface-sunken, var(--surface));
    text-align: left;
    cursor: pointer;
    transition:
      border-color 120ms ease,
      background 120ms ease,
      box-shadow 120ms ease;
  }

  .device-picker-trigger:hover,
  .device-picker-trigger.open {
    border-color: var(--accent);
    background: var(--surface-raised);
  }

  .device-picker-trigger.open {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
  }

  .device-picker-trigger.unavailable {
    border-color: color-mix(in srgb, var(--warning, #d39a55) 55%, var(--line));
  }

  .device-picker-icon,
  .device-option-icon {
    display: grid;
    width: 38px;
    height: 38px;
    place-items: center;
    flex: none;
    border-radius: 11px;
    color: var(--accent-text);
    background: var(--accent-soft);
  }

  .device-picker-value,
  .device-picker-options button > span:nth-child(2) {
    display: grid;
    min-width: 0;
    gap: 3px;
  }

  .device-picker-value strong,
  .device-picker-options strong {
    overflow: hidden;
    color: var(--text);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-picker-value small,
  .device-picker-options small {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.66rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-picker-popover {
    position: absolute;
    z-index: 180;
    top: calc(100% + 8px);
    right: 0;
    left: 0;
    overflow: hidden;
    border: 1px solid var(--line-strong, var(--line));
    border-radius: 15px;
    padding: 8px;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  .device-picker-search {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 7px;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0 10px;
    color: var(--text-muted);
    background: var(--surface-sunken, var(--surface));
  }

  .device-picker-search:focus-within {
    border-color: var(--accent);
  }

  .device-picker-search input {
    width: 100%;
    min-height: 38px;
    border: 0;
    outline: 0;
    padding: 0;
    color: var(--text);
    background: transparent;
    font: inherit;
    font-size: 0.75rem;
  }

  .device-picker-options {
    display: grid;
    max-height: 280px;
    gap: 3px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .device-picker-options button {
    display: grid;
    min-width: 0;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    border: 0;
    border-radius: 11px;
    padding: 8px;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .device-picker-options button:hover,
  .device-picker-options button.selected {
    background: var(--surface-hover, var(--accent-soft));
  }

  .device-picker-options button.selected {
    color: var(--accent-text);
  }

  .device-picker-options p {
    margin: 18px 8px;
    color: var(--text-muted);
    font-size: 0.74rem;
    text-align: center;
  }
</style>
