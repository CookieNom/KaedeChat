<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import CommandSwitcher from '$lib/components/CommandSwitcher.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import {
    browserNotifications,
    shouldOfferBrowserNotificationPrompt,
    type GuildNotificationPreference
  } from '$lib/notifications/browser.svelte';
  import { initializeNativeInstance, isNativeDesktop } from '$lib/platform/native';
  import { guildNavigation } from '$lib/stores/guild-navigation.svelte';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { applyLocale } from '$lib/ui/locale';
  import { applyTheme, type ThemePreference } from '$lib/ui/theme';
  import { onMount } from 'svelte';

  let { children } = $props();
  let notificationSettingsLoaded = $state(false);
  let notificationPromptBusy = $state(false);
  let notificationPromptError = $state('');
  let notificationRetryBusy = $state(false);
  let notificationController: AbortController | null = null;
  let guildPreferenceRequest: Promise<void> | null = null;
  const showNotificationPrompt = $derived(
    notificationSettingsLoaded &&
      shouldOfferBrowserNotificationPrompt(
        browserNotifications.supported,
        browserNotifications.permission,
        browserNotifications.enabled,
        browserNotifications.promptHandled,
        browserNotifications.configured
      )
  );

  interface UserSettingsPayload {
    theme: ThemePreference;
    locale: string;
    presence_preference: 'online' | 'idle' | 'dnd' | 'invisible';
    notification_settings: Record<string, unknown>;
  }

  function requestWasAborted(caught: unknown): boolean {
    return caught instanceof DOMException && caught.name === 'AbortError';
  }

  async function refreshUserSettings(): Promise<void> {
    const controller = notificationController;
    const signal = controller?.signal;
    try {
      const { theme, locale, presence_preference, notification_settings } =
        await api<UserSettingsPayload>('/users/@me/settings', { signal });
      if (controller?.signal.aborted || notificationController !== controller) return;
      applyTheme(theme);
      applyLocale(locale);
      try {
        localStorage.setItem('kaede.presence', presence_preference);
      } catch {
        // The gateway still applies the account preference for this session.
      }
      authenticatedGateway.client.setPresence(presence_preference);
      developerMode.apply(notification_settings);
      browserNotifications.apply(notification_settings);
      notificationSettingsLoaded = true;
      browserNotifications.clearHealthIssue('settings');
    } catch (caught) {
      if (
        requestWasAborted(caught) ||
        controller?.signal.aborted ||
        notificationController !== controller
      )
        return;
      browserNotifications.reportHealthIssue(
        'settings',
        userErrorMessage(
          caught,
          'Could not load notification settings. Notifications are paused until Kaede can retry.'
        )
      );
    }
  }

  function refreshGuildNotificationPreferences(): Promise<void> {
    if (guildPreferenceRequest) return guildPreferenceRequest;
    const controller = notificationController;
    guildPreferenceRequest = api<GuildNotificationPreference[]>(
      '/users/@me/guild-notification-settings',
      { signal: controller?.signal }
    )
      .then((preferences) => {
        if (controller?.signal.aborted || notificationController !== controller) return;
        browserNotifications.applyGuildPreferences(preferences);
      })
      .catch((caught: unknown) => {
        if (
          requestWasAborted(caught) ||
          controller?.signal.aborted ||
          notificationController !== controller
        )
          return;
        browserNotifications.reportHealthIssue(
          'guild-preferences',
          userErrorMessage(
            caught,
            'Could not load guild notification preferences. Notifications are paused to avoid sending the wrong alerts.'
          )
        );
      })
      .finally(() => {
        guildPreferenceRequest = null;
      });
    return guildPreferenceRequest;
  }

  async function retryNotificationHealth(): Promise<void> {
    if (notificationRetryBusy) return;
    notificationRetryBusy = true;
    try {
      if (
        browserNotifications.enabled &&
        browserNotifications.supported &&
        browserNotifications.permission !== 'granted'
      ) {
        await browserNotifications.requestPermission();
      }
      await Promise.all([refreshUserSettings(), refreshGuildNotificationPreferences()]);
      browserNotifications.retryPending();
    } finally {
      notificationRetryBusy = false;
    }
  }

  onMount(() => {
    const controller = new AbortController();
    notificationController = controller;
    let disposed = false;

    function refreshPreferencesWhenBackgrounded() {
      void refreshGuildNotificationPreferences();
      void guildNavigation.load(true);
    }

    developerMode.reset();
    void initializeNativeInstance()
      .then(() => {
        if (!disposed) authenticatedGateway.start();
      })
      .catch((caught: unknown) => {
        if (disposed) return;
        authenticatedGateway.reportStartupFailure(
          userErrorMessage(
            caught,
            'Live updates could not start because the desktop session could not be restored. Unlock the system credential store and reload Kaede.'
          )
        );
      });
    browserNotifications.refreshPromptPreference();
    void refreshUserSettings();
    void refreshGuildNotificationPreferences();
    void guildNavigation.load();
    window.addEventListener('blur', refreshPreferencesWhenBackgrounded);
    document.addEventListener('visibilitychange', refreshPreferencesWhenBackgrounded);
    const guildPreferenceRefreshTimer = window.setInterval(
      refreshGuildNotificationPreferences,
      60_000
    );
    return () => {
      disposed = true;
      controller.abort();
      if (notificationController === controller) notificationController = null;
      window.clearInterval(guildPreferenceRefreshTimer);
      window.removeEventListener('blur', refreshPreferencesWhenBackgrounded);
      document.removeEventListener('visibilitychange', refreshPreferencesWhenBackgrounded);
      authenticatedGateway.stop();
      developerMode.reset();
      browserNotifications.disable();
      guildNavigation.reset();
    };
  });

  function dismissNotificationPrompt() {
    browserNotifications.markPromptHandled();
    notificationPromptError = '';
  }

  async function enableBrowserNotifications() {
    if (notificationPromptBusy) return;
    notificationPromptBusy = true;
    notificationPromptError = '';
    try {
      const permission = await browserNotifications.requestPermission();
      if (permission !== 'granted') {
        notificationPromptError = browserNotifications.permissionError
          ? browserNotifications.permissionError
          : permission === 'denied'
            ? 'Notifications are blocked in this browser’s site permissions. Allow them and try again.'
            : 'Notification permission was not granted. Try again when you are ready.';
        return;
      }
      const latest = await api<UserSettingsPayload>('/users/@me/settings');
      const updated = await api<UserSettingsPayload>('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify({
          notification_settings: {
            ...latest.notification_settings,
            browser_notifications: true
          }
        })
      });
      browserNotifications.apply(updated.notification_settings);
      browserNotifications.markPromptHandled();
    } catch (caught) {
      notificationPromptError = userErrorMessage(
        caught,
        `Could not enable ${isNativeDesktop() ? 'desktop' : 'browser'} notifications. Try again in settings.`
      );
    } finally {
      notificationPromptBusy = false;
    }
  }
</script>

{@render children()}
<CommandSwitcher />

{#if authenticatedGateway.status.state === 'reconnecting' || authenticatedGateway.status.state === 'offline' || authenticatedGateway.status.state === 'degraded'}
  <aside
    class="realtime-status"
    class:offline={authenticatedGateway.status.state === 'offline'}
    class:degraded={authenticatedGateway.status.state === 'degraded'}
    role={authenticatedGateway.status.state === 'offline' ||
    authenticatedGateway.status.state === 'degraded'
      ? 'alert'
      : 'status'}
    aria-live="polite"
  >
    <span aria-hidden="true"
      >{authenticatedGateway.status.state === 'reconnecting' ? '↻' : '!'}</span
    >
    <span>{authenticatedGateway.status.message}</span>
  </aside>
{/if}

{#if showNotificationPrompt || notificationPromptError}
  <aside class="notification-opt-in" aria-labelledby="notification-opt-in-title">
    <div>
      <strong id="notification-opt-in-title">Stay up to date</strong>
      <p>
        Enable {isNativeDesktop() ? 'desktop' : 'browser'} notifications for direct messages and the guild
        alerts you choose. Do Not Disturb silences them.
      </p>
      {#if notificationPromptError}<small role="alert">{notificationPromptError}</small>{/if}
    </div>
    <div class="notification-opt-in-actions">
      <button type="button" disabled={notificationPromptBusy} onclick={dismissNotificationPrompt}
        >Not now</button
      >
      <button
        class="notification-opt-in-primary"
        type="button"
        disabled={notificationPromptBusy}
        onclick={() => void enableBrowserNotifications()}
      >
        {notificationPromptBusy ? 'Enabling…' : 'Enable notifications'}
      </button>
    </div>
  </aside>
{/if}

{#if browserNotifications.health.message}
  <aside class="notification-health" role="alert" aria-live="polite">
    <div>
      <strong>Notifications need attention</strong>
      <p>{browserNotifications.health.message}</p>
      {#if browserNotifications.health.pendingCount > 0}
        <small>
          {browserNotifications.health.pendingCount} notification{browserNotifications.health
            .pendingCount === 1
            ? ''
            : 's'} waiting to be delivered.
        </small>
      {/if}
    </div>
    <button
      type="button"
      disabled={notificationRetryBusy}
      onclick={() => void retryNotificationHealth()}
    >
      {notificationRetryBusy ? 'Retrying…' : 'Retry notifications'}
    </button>
  </aside>
{/if}

<style>
  .realtime-status {
    position: fixed;
    z-index: 126;
    top: 12px;
    left: 50%;
    display: flex;
    max-width: min(560px, calc(100vw - 24px));
    align-items: center;
    gap: 9px;
    transform: translateX(-50%);
    border: 1px solid color-mix(in srgb, var(--warning) 60%, var(--line));
    border-radius: 999px;
    padding: 8px 14px;
    color: var(--text);
    background: color-mix(in srgb, var(--surface-raised) 90%, var(--warning));
    box-shadow: var(--shadow-md);
    font-size: 0.78rem;
    font-weight: 650;
  }

  .realtime-status.offline {
    border-color: color-mix(in srgb, var(--danger) 65%, var(--line));
    background: color-mix(in srgb, var(--surface-raised) 90%, var(--danger));
  }

  .realtime-status.degraded {
    border-color: color-mix(in srgb, var(--warning) 65%, var(--line));
  }

  .notification-opt-in {
    position: fixed;
    z-index: 125;
    right: 18px;
    bottom: 18px;
    display: grid;
    width: min(420px, calc(100vw - 36px));
    gap: 14px;
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 18px;
    background: var(--surface-raised);
    box-shadow: var(--shadow-lg);
  }

  .notification-health {
    position: fixed;
    z-index: 127;
    right: 18px;
    bottom: 18px;
    display: flex;
    width: min(460px, calc(100vw - 36px));
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    border: 1px solid color-mix(in srgb, var(--danger) 60%, var(--line));
    border-radius: 16px;
    padding: 16px;
    background: color-mix(in srgb, var(--surface-raised) 94%, var(--danger));
    box-shadow: var(--shadow-lg);
  }

  .notification-health strong,
  .notification-health p,
  .notification-health small {
    display: block;
  }

  .notification-health p,
  .notification-health small {
    margin: 4px 0 0;
    color: var(--text-muted);
    font-size: 0.76rem;
    line-height: 1.4;
  }

  .notification-health button {
    flex: 0 0 auto;
    min-height: 38px;
    border: 1px solid var(--danger);
    border-radius: 10px;
    padding: 0 13px;
    color: var(--text);
    background: transparent;
    font: inherit;
    font-size: 0.75rem;
    font-weight: 750;
    cursor: pointer;
  }

  .notification-health button:disabled {
    cursor: wait;
    opacity: 0.62;
  }

  .notification-opt-in strong {
    color: var(--text);
    font-size: 0.94rem;
  }

  .notification-opt-in p,
  .notification-opt-in small {
    display: block;
    margin: 5px 0 0;
    color: var(--text-muted);
    font-size: 0.76rem;
    line-height: 1.45;
  }

  .notification-opt-in small {
    color: var(--danger);
  }

  .notification-opt-in-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }

  .notification-opt-in button {
    min-height: 38px;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0 14px;
    color: var(--text-soft);
    background: transparent;
    font: inherit;
    font-size: 0.78rem;
    font-weight: 700;
    cursor: pointer;
  }

  .notification-opt-in button:disabled {
    cursor: wait;
    opacity: 0.62;
  }

  .notification-opt-in .notification-opt-in-primary {
    border-color: var(--accent);
    color: var(--on-accent);
    background: var(--accent);
  }

  @media (max-width: 560px) {
    .notification-opt-in {
      right: 10px;
      bottom: 10px;
      width: calc(100vw - 20px);
    }

    .notification-health {
      right: 10px;
      bottom: 10px;
      width: calc(100vw - 20px);
    }
  }
</style>
