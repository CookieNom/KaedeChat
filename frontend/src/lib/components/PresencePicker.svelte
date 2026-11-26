<script lang="ts">
  import { t } from '$lib/ui/locale';
  import { tick } from 'svelte';
  import {
    PRESENCE_DURATION_OPTIONS,
    formatTimestampHint,
    summarizePresenceSubtext,
    summarizeSchedulesHint,
    type PresencePreference,
    type PresenceStatePayload
  } from '$lib/stores/presence.svelte';
  import Icon from './Icon.svelte';

  let {
    value,
    presenceState = null,
    disabled = false,
    onChange,
    onResetOverride,
    onOpenSchedules
  }: {
    value: PresencePreference;
    presenceState?: PresenceStatePayload | null;
    disabled?: boolean;
    onChange: (value: PresencePreference, durationMinutes?: number | null) => void;
    onResetOverride?: () => void;
    onOpenSchedules?: () => void;
  } = $props();

  let open = $state(false);
  let hoveredSubmenu = $state<PresencePreference | null>(null);
  let mobileDrillStatus = $state<PresencePreference | null>(null);
  let root = $state<HTMLElement | null>(null);
  let triggerButton = $state<HTMLButtonElement | null>(null);
  let menu = $state<HTMLElement | null>(null);
  let menuLeft = $state(12);
  let menuBottom = $state(80);
  let flipFlyoutLeft = $state(false);

  let statuses: Array<{
    value: PresencePreference;
    label: string;
    description: string;
    hasDuration: boolean;
  }> = $derived([
    {
      value: 'online',
      label: $t('ui_online_0d21bd52'),
      description: $t('ui_available_to_chat_14360e53'),
      hasDuration: false
    },
    {
      value: 'idle',
      label: $t('ui_idle_ab0171ca'),
      description: $t('ui_away_for_a_while_ff7263bd'),
      hasDuration: true
    },
    {
      value: 'dnd',
      label: $t('ui_do_not_disturb_fe39f3cd'),
      description: $t('ui_suppress_notifications_8b11705f'),
      hasDuration: true
    },
    {
      value: 'invisible',
      label: $t('ui_invisible_4ee447c5'),
      description: $t('ui_appear_offline_ec36f358'),
      hasDuration: true
    }
  ]);

  const selected = $derived(statuses.find((status) => status.value === value) ?? statuses[0]);
  const triggerSubtext = $derived(summarizePresenceSubtext(presenceState));
  const schedulesHint = $derived(summarizeSchedulesHint(presenceState));
  const flyoutStatus = $derived(hoveredSubmenu ?? mobileDrillStatus);
  const flyoutStatusMeta = $derived(
    statuses.find((status) => status.value === flyoutStatus) ?? null
  );

  function closeMenu() {
    open = false;
    hoveredSubmenu = null;
    mobileDrillStatus = null;
  }

  async function toggle() {
    if (disabled) return;
    open = !open;
    if (open) {
      hoveredSubmenu = null;
      mobileDrillStatus = null;
      if (triggerButton && typeof window !== 'undefined') {
        const bounds = triggerButton.getBoundingClientRect();
        const menuWidth = 280;
        const flyoutWidth = 192;
        const clampedLeft = Math.max(8, Math.min(bounds.left, window.innerWidth - menuWidth - 8));
        menuLeft = clampedLeft;
        menuBottom = Math.max(
          8,
          Math.min(window.innerHeight - 80, window.innerHeight - bounds.top + 8)
        );
        flipFlyoutLeft = clampedLeft + menuWidth + flyoutWidth > window.innerWidth - 8;
      }
      await tick();
      menu?.querySelector<HTMLElement>('[aria-current="true"]')?.focus();
    }
  }

  function choose(next: PresencePreference, durationMinutes?: number | null) {
    if (disabled) return;
    if (durationMinutes === undefined) {
      onChange(next);
    } else {
      onChange(next, durationMinutes);
    }
    closeMenu();
  }

  function isCompactViewport(): boolean {
    return typeof window !== 'undefined' && window.innerWidth <= 700;
  }

  function openDurationFor(status: PresencePreference, event?: MouseEvent) {
    event?.stopPropagation();
    if (isCompactViewport()) {
      mobileDrillStatus = status;
      hoveredSubmenu = null;
    } else {
      hoveredSubmenu = hoveredSubmenu === status ? null : status;
    }
  }

  function menuKeydown(event: KeyboardEvent) {
    const items = Array.from(
      menu?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"], [role="menuitem"]') ?? []
    );
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      if (mobileDrillStatus) {
        mobileDrillStatus = null;
        return;
      }
      closeMenu();
      root?.querySelector<HTMLButtonElement>('.presence-picker-trigger')?.focus();
      return;
    }
    if (event.key === 'ArrowRight') {
      const targetStatus = (document.activeElement as HTMLElement | null)?.dataset.status as
        | PresencePreference
        | undefined;
      if (targetStatus && targetStatus !== 'online') {
        event.preventDefault();
        hoveredSubmenu = targetStatus;
        void tick().then(() => {
          menu?.querySelector<HTMLButtonElement>('.duration-flyout button')?.focus();
        });
      }
      return;
    }
    if (event.key === 'ArrowLeft' && (hoveredSubmenu || mobileDrillStatus)) {
      event.preventDefault();
      const backStatus = hoveredSubmenu ?? mobileDrillStatus;
      hoveredSubmenu = null;
      mobileDrillStatus = null;
      void tick().then(() => {
        menu?.querySelector<HTMLButtonElement>(`[data-status="${backStatus}"]`)?.focus();
      });
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
    if (open && root && !root.contains(event.target as Node)) closeMenu();
  }}
  onkeydown={(event) => {
    if (open && event.key === 'Escape') closeMenu();
  }}
/>

<div bind:this={root} class="presence-picker">
  <button
    bind:this={triggerButton}
    class="presence-picker-trigger"
    type="button"
    {disabled}
    aria-label={`Status: ${selected.label}${triggerSubtext ? ` (${triggerSubtext})` : ''}. Change status`}
    aria-haspopup="menu"
    aria-expanded={open}
    onclick={toggle}
  >
    <i class={`presence-symbol presence-${value}`} aria-hidden="true"></i>
    <span class="trigger-copy">
      <span class="trigger-label">{selected.label}</span>
      {#if triggerSubtext}
        <small class="trigger-subtext">{triggerSubtext}</small>
      {/if}
    </span>
    {#if presenceState?.suppress_notifications && value === 'invisible'}
      <span class="muted-badge" title="Notifications muted by active schedule">Alerts muted</span>
    {/if}
    <svg class="trigger-chevron" viewBox="0 0 16 16" aria-hidden="true"
      ><path d="m4 6 4 4 4-4" /></svg
    >
  </button>

  {#if open}
    <div
      bind:this={menu}
      class="presence-picker-menu"
      style:left={`${menuLeft}px`}
      style:bottom={`${menuBottom}px`}
      role="menu"
      tabindex="-1"
      aria-label={$t('ui_set_your_status_0a1663af')}
      onkeydown={menuKeydown}
      onmouseleave={() => {
        if (!mobileDrillStatus) hoveredSubmenu = null;
      }}
    >
      {#if mobileDrillStatus && flyoutStatusMeta}
        <div class="drill-header">
          <button
            type="button"
            class="drill-back"
            onclick={() => (mobileDrillStatus = null)}
            aria-label="Back to statuses"
          >
            <Icon name="arrow-left" size={14} />
          </button>
          <i class={`presence-symbol presence-${flyoutStatusMeta.value}`} aria-hidden="true"></i>
          <strong>{flyoutStatusMeta.label}</strong>
        </div>
        {#each PRESENCE_DURATION_OPTIONS as option (option.label)}
          <button
            type="button"
            role="menuitem"
            class="duration-option"
            onclick={() => choose(flyoutStatusMeta.value, option.minutes)}
          >
            <span>{option.label}</span>
          </button>
        {/each}
      {:else}
        <header>{$t('ui_your_status_7a7f2d0a')}</header>

        {#each statuses as status (status.value)}
          <div
            class="status-row"
            class:submenu-open={hoveredSubmenu === status.value}
            role="none"
            onmouseenter={() => {
              if (!isCompactViewport()) {
                hoveredSubmenu = status.hasDuration ? status.value : null;
              }
            }}
          >
            <button
              type="button"
              role="menuitemradio"
              class="status-item"
              data-status={status.value}
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
            {#if status.hasDuration}
              <button
                type="button"
                class="duration-chevron"
                aria-label={`Set ${status.label} duration`}
                aria-haspopup="menu"
                aria-expanded={hoveredSubmenu === status.value ||
                  mobileDrillStatus === status.value}
                onclick={(event) => openDurationFor(status.value, event)}
              >
                <Icon name="chevron-right" size={14} />
              </button>
            {/if}
          </div>
        {/each}

        {#if presenceState && (presenceState.source === 'timed' || presenceState.source === 'schedule' || presenceState.source === 'override')}
          <div class="menu-context">
            {#if presenceState.source === 'timed'}
              <span>Timer: {formatTimestampHint(presenceState.timed_expires_at)}</span>
              {#if onResetOverride}
                <button
                  type="button"
                  class="context-action"
                  onclick={() => {
                    onResetOverride();
                    closeMenu();
                  }}
                >
                  Clear
                </button>
              {/if}
            {:else if presenceState.source === 'schedule'}
              <span
                >{presenceState.active_schedule_name} · {formatTimestampHint(
                  presenceState.active_schedule_ends_at
                )}</span
              >
              <button type="button" class="context-action" onclick={() => choose('online')}>
                Skip now
              </button>
            {:else if presenceState.source === 'override'}
              <span>Skipping {presenceState.skipped_schedule_name}</span>
              {#if onResetOverride}
                <button
                  type="button"
                  class="context-action"
                  onclick={() => {
                    onResetOverride();
                    closeMenu();
                  }}
                >
                  Resume
                </button>
              {/if}
            {/if}
          </div>
        {/if}

        {#if onOpenSchedules}
          <div class="menu-divider" role="separator"></div>
          <button
            type="button"
            role="menuitem"
            class="schedules-entry"
            onmouseenter={() => (hoveredSubmenu = null)}
            onclick={() => {
              closeMenu();
              onOpenSchedules();
            }}
          >
            <Icon name="clock" size={15} />
            <span>
              <strong>Schedules</strong>
              <small>{schedulesHint}</small>
            </span>
            <Icon name="chevron-right" size={14} />
          </button>
        {/if}

        {#if hoveredSubmenu && flyoutStatusMeta}
          <div
            class="duration-flyout"
            class:flip-left={flipFlyoutLeft}
            role="menu"
            aria-label={`${flyoutStatusMeta.label} duration`}
          >
            {#each PRESENCE_DURATION_OPTIONS as option (option.label)}
              {@const isCurrentForever =
                option.minutes === null &&
                value === flyoutStatusMeta.value &&
                presenceState?.source === 'manual'}
              <button
                type="button"
                role="menuitem"
                class="duration-option"
                class:active-duration={isCurrentForever}
                onclick={() => choose(flyoutStatusMeta.value, option.minutes)}
              >
                <span>{option.label}</span>
                {#if isCurrentForever}
                  <svg class="presence-check" viewBox="0 0 16 16" aria-hidden="true">
                    <path d="m3 8 3 3 7-7" />
                  </svg>
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .presence-picker {
    position: relative;
    min-width: 0;
  }

  .presence-picker-trigger {
    --presence-cutout: var(--surface-subtle);
    display: inline-flex;
    max-width: 100%;
    min-height: 36px;
    align-items: center;
    gap: 10px;
    border: 0;
    border-radius: 8px;
    padding: 6px 10px;
    color: var(--text-soft);
    background: transparent;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    text-align: left;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .presence-picker-trigger:hover:not(:disabled),
  .presence-picker-trigger[aria-expanded='true'] {
    --presence-cutout: var(--surface-hover);
    color: var(--text);
    background: var(--surface-hover);
  }

  .presence-picker-trigger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .trigger-copy {
    display: grid;
    min-width: 0;
    flex: 1 1 auto;
    gap: 1px;
  }

  .trigger-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 600;
    line-height: 1.25;
  }

  .trigger-subtext {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-muted);
    font-size: 0.74rem;
    font-weight: 500;
    line-height: 1.25;
  }

  .muted-badge {
    display: inline-flex;
    align-items: center;
    flex: 0 0 auto;
    border-radius: 999px;
    padding: 2px 7px;
    background: var(--surface-hover);
    color: var(--text-muted);
    font-size: 0.68rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .trigger-chevron {
    width: 14px;
    height: 14px;
    flex: 0 0 auto;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.9;
    opacity: 0.75;
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
    background: var(--presence-cutout, var(--surface-raised));
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
    background: var(--presence-cutout, var(--surface-raised));
    content: '';
  }

  .presence-invisible {
    box-shadow: inset 0 0 0 2.75px #747f8d;
    background: transparent;
  }

  .presence-picker-menu {
    --presence-cutout: var(--surface-raised);
    position: fixed;
    z-index: 260;
    display: grid;
    width: 280px;
    gap: 2px;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 8px;
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg, 0 20px 54px rgb(0 0 0 / 44%));
  }

  .presence-picker-menu header {
    padding: 6px 10px 6px;
    color: var(--text-muted);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .status-row {
    display: flex;
    align-items: stretch;
    border-radius: 9px;
    transition: background 110ms ease;
  }

  .status-row:hover,
  .status-row.submenu-open {
    --presence-cutout: var(--surface-hover);
    background: var(--surface-hover);
  }

  .status-item {
    display: grid;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 40px;
    grid-template-columns: 18px minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    border: 0;
    border-radius: 9px;
    padding: 8px 6px 8px 10px;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .status-item:hover,
  .status-item:focus-visible {
    color: var(--text);
  }

  .status-item > span {
    display: grid;
    min-width: 0;
    gap: 2px;
  }

  .status-item strong,
  .schedules-entry strong {
    font-size: 0.84rem;
    font-weight: 600;
    line-height: 1.25;
  }

  .status-item small,
  .schedules-entry small {
    color: var(--text-muted);
    font-size: 0.74rem;
    line-height: 1.3;
  }

  .schedules-entry small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .duration-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    flex: 0 0 auto;
    border: 0;
    border-radius: 0 9px 9px 0;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
  }

  .duration-chevron:hover,
  .duration-chevron:focus-visible {
    color: var(--text);
  }

  .presence-check {
    width: 16px;
    height: 16px;
    flex: 0 0 auto;
    fill: none;
    stroke: var(--accent-text);
    stroke-width: 2.2;
  }

  .duration-flyout {
    position: absolute;
    left: calc(100% + 8px);
    bottom: 8px;
    z-index: 265;
    display: grid;
    width: 184px;
    gap: 2px;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 7px;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg, 0 20px 54px rgb(0 0 0 / 48%));
  }

  /* Invisible hover bridge so diagonal pointer travel never closes the flyout */
  .duration-flyout::before {
    position: absolute;
    top: 0;
    bottom: 0;
    left: -12px;
    width: 12px;
    content: '';
  }

  .duration-flyout.flip-left {
    left: auto;
    right: calc(100% + 8px);
  }

  .duration-flyout.flip-left::before {
    left: auto;
    right: -12px;
  }

  .duration-option {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 36px;
    border: 0;
    border-radius: 8px;
    padding: 8px 11px;
    background: transparent;
    color: var(--text-soft);
    font-size: 0.83rem;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
    transition:
      background 110ms ease,
      color 110ms ease;
  }

  .duration-option:hover,
  .duration-option:focus-visible,
  .duration-option.active-duration {
    background: var(--surface-hover);
    color: var(--text);
  }

  .duration-option.active-duration {
    font-weight: 600;
  }

  .drill-header {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 4px 6px 10px;
    border-bottom: 1px solid var(--line-soft);
    margin-bottom: 4px;
    font-size: 0.86rem;
  }

  .drill-back {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--text-soft);
    cursor: pointer;
  }

  .drill-back:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .menu-context {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin: 6px 2px 2px;
    padding: 8px 10px;
    border: 1px solid var(--line-soft);
    border-radius: 9px;
    background: var(--surface-subtle);
    color: var(--text-muted);
    font-size: 0.74rem;
    line-height: 1.3;
  }

  .context-action {
    min-height: 28px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 4px 10px;
    background: var(--surface-raised);
    color: var(--text);
    font-size: 0.73rem;
    font-weight: 600;
    cursor: pointer;
    flex: 0 0 auto;
  }

  .context-action:hover {
    background: var(--surface-hover);
  }

  .menu-divider {
    height: 1px;
    margin: 6px 4px;
    background: var(--line-soft);
  }

  .schedules-entry {
    display: grid;
    min-height: 42px;
    grid-template-columns: 18px minmax(0, 1fr) 16px;
    align-items: center;
    gap: 10px;
    border: 0;
    border-radius: 9px;
    padding: 8px 10px;
    color: var(--text-soft);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .schedules-entry:hover,
  .schedules-entry:focus-visible {
    background: var(--surface-hover);
    color: var(--text);
  }

  .schedules-entry > span {
    display: grid;
    min-width: 0;
    gap: 2px;
  }

  @media (max-width: 700px) {
    .presence-picker-menu {
      width: min(290px, calc(100vw - 24px));
    }

    .duration-flyout {
      display: none;
    }
  }
</style>
