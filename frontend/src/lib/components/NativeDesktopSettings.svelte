<script lang="ts">
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
      notice = 'Kaede is up to date.';
    }
  }

  async function pinToTaskbar(): Promise<void> {
    notice = '';
    await desktopLifecycle.requestTaskbarPin();
    if (!desktopLifecycle.pinError && desktopLifecycle.taskbar?.pinned) {
      notice = 'Kaede is pinned to the taskbar.';
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
      <h2>Desktop app</h2>
      <p>Keep this installed copy current and easy to reach.</p>
    </div>
  </div>
  <div class="settings-card desktop-settings">
    <div class="settings-card-row">
      <div>
        <strong>Application updates</strong>
        <p>
          Kaede checks signed GitHub Releases when it starts and every six hours. Updates install
          only after you approve the restart.
        </p>
        {#if desktopLifecycle.update}
          <small>
            Installed: {desktopLifecycle.update.current_version}{desktopLifecycle.update
              .available && desktopLifecycle.update.version
              ? ` · Available: ${desktopLifecycle.update.version}`
              : ''}
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
          {desktopLifecycle.checking ? 'Checking…' : 'Check for updates'}
        </button>
        {#if desktopLifecycle.update?.available}
          <button
            type="button"
            class="primary-button"
            disabled={desktopLifecycle.installing}
            onclick={() => void desktopLifecycle.installUpdate()}
          >
            {desktopLifecycle.installing ? 'Installing…' : 'Update and restart'}
          </button>
        {/if}
      </div>
    </div>
    {#if desktopLifecycle.updateError}
      <p class="desktop-error" role="alert">{displayError(desktopLifecycle.updateError)}</p>
    {/if}

    <div class="settings-card-row desktop-autostart">
      <div>
        <strong>Launch at sign-in</strong>
        <p>
          Start Kaede in the system tray when you sign in. It stays out of the way and checks for
          updates immediately.
        </p>
      </div>
      <label class="desktop-toggle">
        <input
          type="checkbox"
          checked={desktopLifecycle.autostart?.enabled ?? false}
          disabled={!desktopLifecycle.autostart || desktopLifecycle.savingAutostart}
          onchange={(event) => void desktopLifecycle.setAutostart(event.currentTarget.checked)}
        />
        <span>{desktopLifecycle.autostart?.enabled ? 'On' : 'Off'}</span>
      </label>
    </div>
    {#if desktopLifecycle.autostartError}
      <p class="desktop-error" role="alert">{desktopLifecycle.autostartError}</p>
    {/if}

    {#if desktopLifecycle.taskbar}
      <div class="settings-card-row desktop-taskbar">
        <div>
          <strong>Windows taskbar</strong>
          <p>
            {desktopLifecycle.taskbar.pinned
              ? 'Kaede is already pinned.'
              : desktopLifecycle.taskbar.supported && desktopLifecycle.taskbar.allowed
                ? 'Windows will show its own confirmation before pinning Kaede.'
                : 'Use Pin to taskbar from Kaede’s running taskbar icon on this Windows version.'}
          </p>
        </div>
        {#if !desktopLifecycle.taskbar.pinned && desktopLifecycle.taskbar.supported && desktopLifecycle.taskbar.allowed}
          <button
            type="button"
            class="secondary-button"
            disabled={desktopLifecycle.pinning}
            onclick={() => void pinToTaskbar()}
          >
            {desktopLifecycle.pinning ? 'Asking Windows…' : 'Pin to taskbar'}
          </button>
        {/if}
      </div>
    {/if}
    {#if desktopLifecycle.pinError}
      <p class="desktop-error" role="alert">{displayError(desktopLifecycle.pinError)}</p>
    {/if}
    {#if notice}<p class="desktop-notice" role="status">{notice}</p>{/if}
    <p class="settings-helper">
      On Windows, Kaede installs per-user in Local AppData and appears in Windows Installed apps for
      normal uninstallation. The installer lets you choose whether to add a Start menu shortcut.
    </p>
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
