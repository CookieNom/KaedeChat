<script lang="ts">
  import { t } from '$lib/ui/locale';

  import {
    desktopLifecycle,
    NATIVE_UPDATE_POLL_INTERVAL_MS
  } from '$lib/platform/desktop-lifecycle.svelte';
  import { isNativeDesktop } from '$lib/platform/native';
  import { onMount } from 'svelte';

  onMount(() => {
    if (!isNativeDesktop()) return;
    void desktopLifecycle.initialize();
    const timer = window.setInterval(
      () => void desktopLifecycle.checkForUpdates(false),
      NATIVE_UPDATE_POLL_INTERVAL_MS
    );
    return () => window.clearInterval(timer);
  });
</script>

{#if desktopLifecycle.update?.available && desktopLifecycle.update.version !== desktopLifecycle.dismissedUpdateVersion}
  <aside class="desktop-action" aria-labelledby="desktop-update-title" role="status">
    <div>
      <strong id="desktop-update-title"
        >{$t('ui_kaede_value0_is_ready_970dec99', {
          value0: String(desktopLifecycle.update.version)
        })}</strong
      >
      <p>{$t('ui_a_signed_update_was_found_on_github_releases__f2db5b16')}</p>
      {#if desktopLifecycle.updateError}<small role="alert">{desktopLifecycle.updateError}</small
        >{/if}
    </div>
    <div class="desktop-action-buttons">
      <button
        type="button"
        disabled={desktopLifecycle.installing}
        onclick={() => desktopLifecycle.dismissUpdate()}>{$t('ui_later_73b6e48a')}</button
      >
      <button
        type="button"
        class="primary"
        disabled={desktopLifecycle.installing}
        onclick={() => void desktopLifecycle.installUpdate()}
      >
        {desktopLifecycle.installing
          ? $t('ui_installing_530bcc35')
          : $t('ui_update_and_restart_853c6a3c')}
      </button>
    </div>
  </aside>
{:else if desktopLifecycle.showTaskbarPrompt}
  <aside class="desktop-action" aria-labelledby="desktop-pin-title">
    <div>
      <strong id="desktop-pin-title">{$t('ui_keep_kaede_close_b0da074a')}</strong>
      <p>{$t('ui_would_you_like_to_ask_windows_to_pin_kaede_to_1b9bc46c')}</p>
      {#if desktopLifecycle.pinError}<small role="alert">{desktopLifecycle.pinError}</small>{/if}
    </div>
    <div class="desktop-action-buttons">
      <button
        type="button"
        disabled={desktopLifecycle.pinning}
        onclick={() => desktopLifecycle.dismissTaskbarPrompt()}>{$t('ui_not_now_a0e63d7c')}</button
      >
      <button
        type="button"
        class="primary"
        disabled={desktopLifecycle.pinning}
        onclick={() => void desktopLifecycle.requestTaskbarPin()}
      >
        {desktopLifecycle.pinning
          ? $t('ui_asking_windows_35238601')
          : $t('ui_pin_to_taskbar_4ea2fc5b')}
      </button>
    </div>
  </aside>
{/if}

<style>
  .desktop-action {
    position: fixed;
    z-index: 140;
    right: 18px;
    bottom: 18px;
    display: flex;
    align-items: center;
    gap: 18px;
    width: min(620px, calc(100vw - 36px));
    padding: 16px 18px;
    border: 1px solid color-mix(in srgb, var(--accent) 55%, var(--line));
    border-radius: 18px;
    color: var(--text);
    background: color-mix(in srgb, var(--surface) 94%, var(--accent-soft));
    box-shadow: 0 18px 54px rgb(0 0 0 / 30%);
  }

  .desktop-action > div:first-child {
    min-width: 0;
    flex: 1;
  }

  .desktop-action strong,
  .desktop-action p,
  .desktop-action small {
    display: block;
  }

  .desktop-action p,
  .desktop-action small {
    margin: 4px 0 0;
    color: var(--muted);
  }

  .desktop-action small {
    color: var(--danger);
  }

  .desktop-action-buttons {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }

  .desktop-action button {
    min-height: 42px;
    padding: 0 16px;
    border: 1px solid var(--line-strong);
    border-radius: 12px;
    color: var(--text);
    background: var(--surface-raised);
  }

  .desktop-action button.primary {
    border-color: var(--accent);
    color: var(--on-accent);
    background: var(--accent);
  }

  @media (max-width: 640px) {
    .desktop-action {
      right: 10px;
      bottom: 10px;
      align-items: stretch;
      flex-direction: column;
      width: calc(100vw - 20px);
    }

    .desktop-action-buttons {
      justify-content: flex-end;
    }
  }
</style>
