<script lang="ts">
  import Icon from '$lib/components/Icon.svelte';
  import ListeningVolume from './ListeningVolume.svelte';
  let {
    name,
    voice = 1,
    stream = 1,
    hasStream = false,
    onChange
  }: {
    name: string;
    voice?: number;
    stream?: number;
    hasStream?: boolean;
    onChange: (stream: boolean, value: number) => Promise<void>;
  } = $props();
  const id = $props.id();
  let panel = $state<HTMLDivElement>();
  let expanded = $state(false);
  let left = $state(12);
  let bottom = $state(12);
  function toggle(event: MouseEvent) {
    if (expanded) {
      panel?.hidePopover();
      return;
    }
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    left = Math.max(12, Math.min(bounds.right - 240, window.innerWidth - 252));
    bottom = Math.max(
      12,
      Math.min(window.innerHeight - bounds.top + 8, window.innerHeight - (hasStream ? 210 : 150))
    );
    panel?.showPopover();
  }
</script>

<div class="audio-menu">
  <button
    onclick={toggle}
    aria-expanded={expanded}
    aria-controls={id}
    aria-label={`Audio controls for ${name}`}
    title={`Audio controls for ${name}`}
  >
    <Icon name="volume" size={18} /><Icon name="chevron-down" size={12} />
  </button>
  <div
    {id}
    bind:this={panel}
    class="audio-panel"
    popover="auto"
    role="dialog"
    aria-label={`Audio controls for ${name}`}
    style:left={`${left}px`}
    style:bottom={`${bottom}px`}
    ontoggle={(event) => (expanded = event.newState === 'open')}
  >
    <strong>{name}</strong><small>Only changes what you hear</small>
    <ListeningVolume
      label="User volume"
      value={voice}
      onChange={(value) => onChange(false, value)}
    />
    {#if hasStream}<ListeningVolume
        label="Stream volume"
        value={stream}
        onChange={(value) => onChange(true, value)}
      />{/if}
  </div>
</div>

<style>
  .audio-menu {
    position: absolute;
    right: 10px;
    bottom: 10px;
    z-index: 3;
  }
  button {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-width: 44px;
    min-height: 36px;
    box-sizing: border-box;
    padding: 8px;
    border: 1px solid #ffffff26;
    border-radius: 8px;
    color: white;
    background: #17171ce6;
    cursor: pointer;
    list-style: none;
  }
  button::-webkit-details-marker {
    display: none;
  }
  button:hover,
  button[aria-expanded='true'] {
    background: #34343e;
  }
  button:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }
  .audio-panel {
    position: fixed;
    inset: auto;
    margin: 0;
    max-height: calc(100dvh - 24px);
    overflow-y: auto;
    width: min(240px, calc(100vw - 48px));
    padding: 12px;
    box-sizing: border-box;
    background: var(--surface-raised);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 12px;
    box-shadow: 0 8px 24px #0006;
  }
  strong,
  small {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  strong {
    font-size: 13px;
  }
  small {
    margin: 4px 0 12px;
    font-size: 11px;
    color: var(--text-muted);
  }
</style>
