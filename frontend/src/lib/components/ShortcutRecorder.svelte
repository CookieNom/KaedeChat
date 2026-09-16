<script lang="ts">
  import Icon from './Icon.svelte';
  let { label, value = $bindable() }: { label: string; value?: string | null } = $props();
  let recording = $state(false);
  function record(event: KeyboardEvent) {
    if (!recording) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'Escape') {
      recording = false;
      return;
    }
    if (event.repeat || ['Control', 'Shift', 'Alt', 'Meta'].includes(event.key) || !event.code)
      return;
    const key = event.code.replace(/^(Key|Digit)/, '');
    value = [
      event.ctrlKey && 'Ctrl',
      event.altKey && 'Alt',
      event.shiftKey && 'Shift',
      event.metaKey && 'Super',
      key
    ]
      .filter(Boolean)
      .join('+');
    recording = false;
  }
</script>

<div class="shortcut-field">
  <span class="shortcut-label">{label}</span>
  <div class:recording class="recorder">
    <button
      class="record"
      aria-label={`Record ${label.toLowerCase()} shortcut`}
      aria-pressed={recording}
      onclick={() => (recording = !recording)}
      onkeydown={record}
      onblur={() => (recording = false)}
    >
      {#if recording}<span>Press a key combination…</span>{:else if value}<span class="keys"
          >{#each value.split('+') as key, index (index)}<kbd>{key}</kbd>{/each}</span
        >{:else}<span class="placeholder">Record shortcut</span>{/if}
      <Icon name="key" size={16} />
    </button>
    {#if value}<button
        class="clear"
        aria-label={`Clear ${label.toLowerCase()} shortcut`}
        title="Clear shortcut"
        onclick={() => (value = null)}><Icon name="x" size={16} /></button
      >{/if}
  </div>
  {#if recording}<small role="status">Press Escape to cancel</small>{/if}
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
  .clear:hover {
    color: var(--danger);
    background: var(--surface-hover);
  }
</style>
