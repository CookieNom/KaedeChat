<script lang="ts">
  import { api } from '$lib/api/client';
  import CommandSwitcher from '$lib/components/CommandSwitcher.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import {
    browserNotifications,
    shouldOfferBrowserNotificationPrompt,
    type GuildNotificationPreference
  } from '$lib/notifications/browser.svelte';
  import { initializeNativeInstance, isNativeDesktop } from '$lib/platform/native';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { applyLocale } from '$lib/ui/locale';
  import { applyTheme, type ThemePreference } from '$lib/ui/theme';
  import { onMount } from 'svelte';

  let { children } = $props();
  let notificationSettingsLoaded = $state(false);
  let notificationPromptBusy = $state(false);
  let notificationPromptError = $state('');
  const showNotificationPrompt = $derived(
    notificationSettingsLoaded &&
      shouldOfferBrowserNotificationPrompt(
        browserNotifications.supported,
        browserNotifications.permission,
        browserNotifications.enabled,
        browserNotifications.promptHandled
      )
  );

  interface UserSettingsPayload {
    theme: ThemePreference;
    locale: string;
    presence_preference: 'online' | 'idle' | 'dnd' | 'invisible';
    notification_settings: Record<string, unknown>;
  }

  onMount(() => {
    const controller = new AbortController();
    let disposed = false;
    let guildPreferenceRequest: Promise<void> | null = null;

    function refreshGuildNotificationPreferences(): Promise<void> {
      if (guildPreferenceRequest) return guildPreferenceRequest;
      guildPreferenceRequest = api<GuildNotificationPreference[]>(
        '/users/@me/guild-notification-settings',
        { signal: controller.signal }
      )
        .then((preferences) => browserNotifications.applyGuildPreferences(preferences))
        .catch(() => {
          // Keep the previous snapshot. A later focus change or periodic refresh will retry.
        })
        .finally(() => {
          guildPreferenceRequest = null;
        });
      return guildPreferenceRequest;
    }

    function refreshPreferencesWhenBackgrounded() {
      void refreshGuildNotificationPreferences();
    }

    developerMode.reset();
    void initializeNativeInstance()
      .then(() => {
        if (!disposed) authenticatedGateway.start();
      })
      .catch(() => {
        // Protected API requests surface a durable-vault error if restoration
        // remains unavailable. Do not start an unauthenticated gateway first.
      });
    browserNotifications.refreshPromptPreference();
    void api<UserSettingsPayload>('/users/@me/settings', {
      signal: controller.signal
    })
      .then(({ theme, locale, presence_preference, notification_settings }) => {
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
      })
      .catch(() => {
        // The route's own session guard and error state handle unavailable APIs.
      });
    void refreshGuildNotificationPreferences();
    window.addEventListener('blur', refreshPreferencesWhenBackgrounded);
    document.addEventListener('visibilitychange', refreshPreferencesWhenBackgrounded);
    const guildPreferenceRefreshTimer = window.setInterval(
      refreshGuildNotificationPreferences,
      60_000
    );
    return () => {
      disposed = true;
      controller.abort();
      window.clearInterval(guildPreferenceRefreshTimer);
      window.removeEventListener('blur', refreshPreferencesWhenBackgrounded);
      document.removeEventListener('visibilitychange', refreshPreferencesWhenBackgrounded);
      authenticatedGateway.stop();
      developerMode.reset();
      browserNotifications.disable();
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
        notificationPromptError =
          permission === 'denied'
            ? 'Notifications are blocked in this browser’s site permissions.'
            : 'Notification permission was not granted.';
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
    } catch {
      notificationPromptError = `Could not enable ${isNativeDesktop() ? 'desktop' : 'browser'} notifications. Try again in settings.`;
    } finally {
      notificationPromptBusy = false;
    }
  }
</script>

{@render children()}
<CommandSwitcher />

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

<style>
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
  }
</style>
