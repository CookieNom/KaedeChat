<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { desktopLifecycle } from '$lib/platform/desktop-lifecycle.svelte';
  import { nativeError } from '$lib/platform/native';
  import { onMount } from 'svelte';

  let notice = $state('');

  onMount(() => {
    void desktopLifecycle.refreshTaskbarStatus(false);
    void desktopLifecycle.refreshAutostartStatus();
    if (!desktopLifecycle.update && !desktopLifecycle.checking) {
      void desktopLifecycle.checkForUpdates(false);
    }
  });

  async function checkForUpdates(): Promise<void> {
    notice = '';
    await desktopLifecycle.checkForUpdates(true);
    if (
      !desktopLifecycle.updateError &&
      desktopLifecycle.update?.supported &&
      !desktopLifecycle.update.available
    ) {
      notice = $t('ui_kaede_is_up_to_date_f8acf878');
    }
  }

  async function pinToTaskbar(): Promise<void> {
    notice = '';
    await desktopLifecycle.requestTaskbarPin();
    if (!desktopLifecycle.pinError && desktopLifecycle.taskbar?.pinned) {
      notice = $t('ui_kaede_is_pinned_to_the_taskbar_0d85b3e5');
    }
  }

  function displayError(value: unknown): string {
    return nativeError(value).message ?? 'The desktop operation could not be completed.';
  }
</script>

<section id="desktop-app" class="settings-section">
  <div class="settings-section-heading">
    <span class="section-icon" aria-hidden="true">↻</span>
    <div>
      <h2>{$t('ui_desktop_app_71028420')}</h2>
      <p>{$t('ui_keep_this_installed_copy_current_and_easy_to__7e01bce7')}</p>
    </div>
  </div>
  <div class="settings-card desktop-settings">
    <div class="settings-card-row">
      <div>
        <strong>{$t('ui_application_updates_4dae8ce0')}</strong>
        <p>{$t('ui_kaede_checks_signed_github_releases_when_it_s_0a4665fd')}</p>
        {#if desktopLifecycle.update}
          <small>
            {$t('ui_installed_value0_value1_7fdaca2a', {
              value0: String(desktopLifecycle.update.current_version),
              value1: String(
                desktopLifecycle.update.available && desktopLifecycle.update.version
                  ? ` · Available: ${desktopLifecycle.update.version}`
                  : ''
              )
            })}
          </small>
          {#if desktopLifecycle.update.support_message}
            <small>{desktopLifecycle.update.support_message}</small>
          {/if}
        {/if}
      </div>
      <div class="desktop-buttons">
        <button
          type="button"
          class="secondary-button"
          disabled={desktopLifecycle.checking || desktopLifecycle.installing}
          onclick={() => void checkForUpdates()}
        >
          {desktopLifecycle.checking
            ? $t('ui_checking_ec963ffc')
            : $t('ui_check_for_updates_f26f3275')}
        </button>
        {#if desktopLifecycle.update?.available}
          <button
            type="button"
            class="primary-button"
            disabled={desktopLifecycle.installing}
            onclick={() => void desktopLifecycle.installUpdate()}
          >
            {desktopLifecycle.installing
              ? $t('ui_installing_530bcc35')
              : $t('ui_update_and_restart_853c6a3c')}
          </button>
        {/if}
      </div>
    </div>
    {#if desktopLifecycle.updateError}
      <p class="desktop-error" role="alert">{displayError(desktopLifecycle.updateError)}</p>
    {/if}

    <div class="settings-card-row desktop-autostart">
      <div>
        <strong>{$t('ui_launch_at_sign_in_ae13d4e8')}</strong>
        <p>{$t('ui_start_kaede_in_the_system_tray_when_you_sign__09b74933')}</p>
      </div>
      <label class="desktop-toggle">
        <input
          type="checkbox"
          checked={desktopLifecycle.autostart?.enabled ?? false}
          disabled={!desktopLifecycle.autostart || desktopLifecycle.savingAutostart}
          onchange={(event) => void desktopLifecycle.setAutostart(event.currentTarget.checked)}
        />
        <span
          >{desktopLifecycle.autostart?.enabled
            ? $t('ui_on_13001175')
            : $t('ui_off_ca7981b4')}</span
        >
      </label>
    </div>
    {#if desktopLifecycle.autostartError}
      <p class="desktop-error" role="alert">{desktopLifecycle.autostartError}</p>
    {/if}

    {#if desktopLifecycle.taskbar?.supported && (desktopLifecycle.taskbar.allowed || desktopLifecycle.taskbar.pinned)}
      <div class="settings-card-row desktop-taskbar">
        <div>
          <strong>{$t('ui_windows_taskbar_63eeece9')}</strong>
          <p>
            {desktopLifecycle.taskbar.pinned
              ? $t('ui_kaede_is_already_pinned_4941bec5')
              : $t('ui_windows_will_show_its_own_confirmation_before_a76f7dcc')}
          </p>
        </div>
        {#if !desktopLifecycle.taskbar.pinned && desktopLifecycle.taskbar.supported && desktopLifecycle.taskbar.allowed}
          <button
            type="button"
            class="secondary-button"
            disabled={desktopLifecycle.pinning}
            onclick={() => void pinToTaskbar()}
          >
            {desktopLifecycle.pinning
              ? $t('ui_asking_windows_35238601')
              : $t('ui_pin_to_taskbar_4ea2fc5b')}
          </button>
        {/if}
      </div>
    {/if}
    {#if desktopLifecycle.pinError}
      <p class="desktop-error" role="alert">{displayError(desktopLifecycle.pinError)}</p>
    {/if}
    {#if notice}<p class="desktop-notice" role="status">{notice}</p>{/if}
    <p class="settings-helper">{$t('ui_on_windows_kaede_installs_per_user_in_local_a_d1a3907f')}</p>
  </div>
</section>

<style>
  .desktop-settings,
  .desktop-buttons {
    display: grid;
    gap: 12px;
  }

  .desktop-buttons {
    justify-items: end;
  }

  .desktop-autostart,
  .desktop-taskbar {
    padding-top: 18px;
    border-top: 1px solid var(--line);
  }

  .desktop-toggle {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    font-weight: 700;
    cursor: pointer;
  }

  .desktop-toggle input {
    width: 20px;
    height: 20px;
    accent-color: var(--accent);
  }

  .desktop-toggle:has(input:disabled) {
    cursor: wait;
    opacity: 0.7;
  }

  .desktop-error,
  .desktop-notice {
    margin: 0;
    font-weight: 700;
  }

  .desktop-error {
    color: var(--danger);
  }

  .desktop-notice {
    color: var(--success);
  }

  @media (max-width: 720px) {
    .desktop-buttons {
      justify-items: stretch;
    }
  }
</style>
