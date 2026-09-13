<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { tick } from 'svelte';

  export type PresencePreference = 'online' | 'idle' | 'dnd' | 'invisible';

  let {
    value,
    onChange
  }: {
    value: PresencePreference;
    onChange: (value: PresencePreference) => void;
  } = $props();

  let open = $state(false);
  let root = $state<HTMLElement | null>(null);
  let menu = $state<HTMLElement | null>(null);

  let statuses: Array<{
    value: PresencePreference;
    label: string;
    description: string;
  }> = $derived([
    {
      value: 'online',
      label: $t('ui_online_0d21bd52'),
      description: $t('ui_available_to_chat_14360e53')
    },
    {
      value: 'idle',
      label: $t('ui_idle_ab0171ca'),
      description: $t('ui_away_for_a_while_ff7263bd')
    },
    {
      value: 'dnd',
      label: $t('ui_do_not_disturb_fe39f3cd'),
      description: $t('ui_suppress_notifications_8b11705f')
    },
    {
      value: 'invisible',
      label: $t('ui_invisible_4ee447c5'),
      description: $t('ui_appear_offline_ec36f358')
    }
  ]);

  const selected = $derived(statuses.find((status) => status.value === value) ?? statuses[0]);

  async function toggle() {
    open = !open;
    if (open) {
      await tick();
      menu?.querySelector<HTMLElement>('[aria-current="true"]')?.focus();
    }
  }

  function choose(next: PresencePreference) {
    onChange(next);
    open = false;
  }

  function menuKeydown(event: KeyboardEvent) {
    const items = Array.from(menu?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      open = false;
      root?.querySelector<HTMLButtonElement>('.presence-picker-trigger')?.focus();
      return;
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const direction = event.key === 'ArrowDown' ? 1 : -1;
    items[(current + direction + items.length) % items.length]?.focus();
  }
</script>

<svelte:window
  onclick={(event) => {
    if (open && root && !root.contains(event.target as Node)) open = false;
  }}
  onkeydown={(event) => {
    if (open && event.key === 'Escape') open = false;
  }}
/>

<div bind:this={root} class="presence-picker">
  <button
    class="presence-picker-trigger"
    type="button"
    aria-label={`Status: ${selected.label}. Change status`}
    aria-haspopup="menu"
    aria-expanded={open}
    onclick={toggle}
  >
    <i class={`presence-symbol presence-${value}`} aria-hidden="true"></i>
    <span>{selected.label}</span>
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
  </button>

  {#if open}
    <div
      bind:this={menu}
      class="presence-picker-menu"
      role="menu"
      tabindex="-1"
      aria-label={$t('ui_set_your_status_0a1663af')}
      onkeydown={menuKeydown}
    >
      <header>{$t('ui_your_status_7a7f2d0a')}</header>
      {#each statuses as status (status.value)}
        <button
          type="button"
          role="menuitemradio"
          aria-checked={value === status.value}
          aria-current={value === status.value ? 'true' : undefined}
          onclick={() => choose(status.value)}
        >
          <i class={`presence-symbol presence-${status.value}`} aria-hidden="true"></i>
          <span><strong>{status.label}</strong><small>{status.description}</small></span>
          {#if value === status.value}
            <svg class="presence-check" viewBox="0 0 16 16" aria-hidden="true">
              <path d="m3 8 3 3 7-7" />
            </svg>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .presence-picker {
    position: relative;
    min-width: 0;
  }

  .presence-picker-trigger {
    display: inline-flex;
    max-width: 100%;
    min-height: 24px;
    align-items: center;
    gap: 6px;
    border: 0;
    border-radius: 6px;
    padding: 2px 5px;
    color: var(--text-muted);
    background: transparent;
    font-size: 0.63rem;
    cursor: pointer;
  }

  .presence-picker-trigger:hover,
  .presence-picker-trigger[aria-expanded='true'] {
    color: var(--text-soft);
    background: var(--surface-hover);
  }

  .presence-picker-trigger > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .presence-picker-trigger > svg {
    width: 12px;
    height: 12px;
    flex: 0 0 auto;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
  }

  .presence-symbol {
    position: relative;
    display: inline-block;
    width: 10px;
    height: 10px;
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
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--surface-raised);
    content: '';
  }

  .presence-dnd {
    background: #ed4245;
  }

  .presence-dnd::after {
    position: absolute;
    top: 4px;
    left: 2px;
    width: 6px;
    height: 2px;
    border-radius: 2px;
    background: var(--surface-raised);
    content: '';
  }

  .presence-invisible {
    box-shadow: inset 0 0 0 3px #747f8d;
    background: transparent;
  }

  .presence-picker-menu {
    position: absolute;
    z-index: 230;
    bottom: calc(100% + 10px);
    left: -8px;
    display: grid;
    width: 250px;
    gap: 2px;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 6px;
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: 0 18px 48px rgb(0 0 0 / 44%);
  }

  .presence-picker-menu header {
    padding: 7px 9px 5px;
    color: var(--text-muted);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .presence-picker-menu button {
    display: grid;
    grid-template-columns: 18px minmax(0, 1fr) 18px;
    align-items: center;
    gap: 8px;
    border: 0;
    border-radius: 8px;
    padding: 8px 9px;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .presence-picker-menu button:hover,
  .presence-picker-menu button:focus-visible {
    color: var(--text);
    background: var(--surface-hover);
  }

  .presence-picker-menu button > span {
    display: grid;
    min-width: 0;
    gap: 1px;
  }

  .presence-picker-menu strong {
    font-size: 0.71rem;
  }

  .presence-picker-menu small {
    color: var(--text-muted);
    font-size: 0.61rem;
  }

  .presence-check {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: var(--accent-text);
    stroke-width: 2;
  }

  @media (max-width: 700px) {
    .presence-picker-menu {
      width: min(250px, calc(100vw - 28px));
    }
  }
</style>
