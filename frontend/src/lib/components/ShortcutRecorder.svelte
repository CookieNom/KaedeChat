<script lang="ts">
  import Icon from './Icon.svelte';
  let {
    label,
    value = $bindable(),
    disabled = false,
    onsave
  }: {
    label: string;
    value?: string | null;
    disabled?: boolean;
    onsave?: () => void;
  } = $props();
  let recording = $state(false);
  let pending = $state<string | null>(null);
  function record(event: KeyboardEvent) {
    if (!recording || disabled) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'Escape') {
      recording = false;
      return;
    }
    if (event.repeat || ['Control', 'Shift', 'Alt', 'Meta'].includes(event.key) || !event.code)
      return;
    const key = event.code.replace(/^(Key|Digit)/, '');
    pending = [
      event.ctrlKey && 'Ctrl',
      event.altKey && 'Alt',
      event.shiftKey && 'Shift',
      event.metaKey && 'Super',
      key
    ]
      .filter(Boolean)
      .join('+');
  }
</script>

<div class="shortcut-field">
  <span class="shortcut-label">{label}</span>
  <div class:recording class="recorder">
    <button
      class="record"
      type="button"
      {disabled}
      aria-label={`Record ${label.toLowerCase()} shortcut`}
      aria-pressed={recording}
      onclick={() => {
        pending = null;
        recording = true;
      }}
      onkeydown={record}
    >
      {#if recording && !pending}<span>Press a single key or key combination…</span
        >{:else if recording ? pending : value}<span class="keys"
          >{#each (recording ? pending! : value!).split('+') as key, index (index)}<kbd>{key}</kbd
            >{/each}</span
        >{:else}<span class="placeholder">Record shortcut</span>{/if}
      <Icon name="key" size={16} />
    </button>
    {#if value && !recording}<button
        type="button"
        {disabled}
        class="clear"
        aria-label={`Clear ${label.toLowerCase()} shortcut`}
        title="Clear shortcut"
        onclick={() => (value = null)}><Icon name="x" size={16} /></button
      >{/if}
  </div>
  {#if recording}
    <small role="status"
      >{pending
        ? 'Click Save keybind to finish, or press another key to replace it.'
        : 'Waiting for a key. Save becomes available after you press a key.'}</small
    >
    <div class="recording-actions">
      <button
        type="button"
        class="save"
        disabled={disabled || !pending}
        onclick={() => {
          if (disabled || !pending) return;
          value = pending;
          recording = false;
          onsave?.();
        }}>Save keybind</button
      >
      <button type="button" {disabled} onclick={() => (recording = false)}>Cancel</button>
    </div>
  {:else}
    <small>Click the box to record a keybind.</small>
  {/if}
</div>

<style>
  .shortcut-field {
    display: grid;
    align-content: start;
    gap: 8px;
    min-width: 0;
  }
  .shortcut-label {
    font-size: 13px;
    font-weight: 600;
  }
  .recorder {
    display: flex;
    min-height: 44px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface-subtle);
  }
  .recorder.recording {
    border-color: var(--danger);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger) 16%, transparent);
  }
  button {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    background: transparent;
    border: 0;
    color: var(--text);
    cursor: pointer;
    border-radius: 8px;
    font: inherit;
    font-size: 12px;
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .record {
    justify-content: space-between;
    flex: 1;
    min-width: 0;
  }
  .keys {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  kbd {
    border: 1px solid var(--line);
    border-bottom-width: 2px;
    border-radius: 4px;
    padding: 2px 5px;
    background: var(--surface-raised);
    font: inherit;
    font-size: 11px;
  }
  .placeholder,
  small {
    color: var(--text-muted);
  }
  small {
    font-size: 11px;
  }
  .recording-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .recording-actions button {
    min-height: 44px;
    border: 1px solid var(--line);
    background: var(--surface-raised);
  }
  .recording-actions .save {
    background: var(--accent);
    color: var(--on-accent, #fff);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .clear:hover {
    color: var(--danger);
    background: var(--surface-hover);
  }
</style>
