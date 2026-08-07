<script lang="ts">
  import { api } from '$lib/api/client';
  import CommandSwitcher from '$lib/components/CommandSwitcher.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import {
    browserNotifications,
    shouldOfferBrowserNotificationPrompt,
    type GuildNotificationPreference
  } from '$lib/notifications/browser.svelte';
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
    notification_settings: Record<string, unknown>;
  }

  onMount(() => {
    const controller = new AbortController();
    developerMode.reset();
    authenticatedGateway.start();
    browserNotifications.refreshPromptPreference();
    void api<UserSettingsPayload>('/users/@me/settings', {
      signal: controller.signal
    })
      .then(({ theme, locale, notification_settings }) => {
        applyTheme(theme);
        applyLocale(locale);
        developerMode.apply(notification_settings);
        browserNotifications.apply(notification_settings);
        notificationSettingsLoaded = true;
      })
      .catch(() => {
        // The route's own session guard and error state handle unavailable APIs.
      });
    void api<GuildNotificationPreference[]>('/users/@me/guild-notification-settings', {
      signal: controller.signal
    })
      .then((preferences) => browserNotifications.applyGuildPreferences(preferences))
      .catch(() => {
        // Guild notifications remain suppressed until preferences can be loaded safely.
      });
    return () => {
      controller.abort();
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
      notificationPromptError = 'Could not enable browser notifications. Try again in settings.';
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
      <p>Enable browser notifications for direct messages and the guild alerts you choose.</p>
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
