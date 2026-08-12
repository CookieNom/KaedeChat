<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, expireBrowserSession, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import type { UserSummary } from '$lib/chat/types';
  import Icon from '$lib/components/Icon.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import NativeVoiceSettings from '$lib/components/NativeVoiceSettings.svelte';
  import { isNativeDesktop, nativeError, nativeInvoke } from '$lib/platform/native';
  import { assetUrl } from '$lib/media/assets';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import {
    browserNotifications,
    browserNotificationsFromSettings
  } from '$lib/notifications/browser.svelte';
  import { developerMode, developerModeFromSettings } from '$lib/ui/developer-mode.svelte';
  import { applyLocale } from '$lib/ui/locale';
  import { applyTheme, type ThemePreference } from '$lib/ui/theme';
  import { onMount } from 'svelte';

  interface UserProfile extends UserSummary {
    banner_hash: string | null;
    bio: string | null;
    custom_status: string | null;
    email: string | null;
    email_verified: boolean;
    mfa_enabled: boolean;
  }

  interface UserSettings {
    locale: string;
    theme: ThemePreference;
    dm_privacy: 'everyone' | 'shared_guild' | 'friends';
    notification_settings: Record<string, unknown>;
  }

  interface MfaSetup {
    secret: string;
    uri: string;
  }

  let profile = $state<UserProfile | null>(null);
  let settings = $state<UserSettings>({
    locale: 'en-US',
    theme: 'system',
    dm_privacy: 'shared_guild',
    notification_settings: {}
  });
  let emailEnabled = $state<boolean | null>(null);
  let notice = $state('');
  let error = $state('');
  let loaded = $state(false);
  let busy = $state(false);
  let savedTheme = $state<UserSettings['theme']>('system');
  let assetProgress = $state(0);
  let lifecycle = 0;
  let routeController: AbortController | null = null;
  let displayName = $state('');
  let bio = $state('');
  let customStatus = $state('');
  let developerModeDraft = $state(false);
  let browserNotificationsDraft = $state(false);
  let testingNotification = $state(false);

  let nextEmail = $state('');
  let emailPassword = $state('');
  let mfaPassword = $state('');
  let mfaCurrentCode = $state('');
  let mfaCode = $state('');
  let mfaSetup = $state<MfaSetup | null>(null);
  let recoveryCodes = $state<string[]>([]);
  let disablePassword = $state('');
  let disableCode = $state('');

  onMount(() => {
    const generation = ++lifecycle;
    const controller = new AbortController();
    routeController = controller;
    void Promise.all([
      api<UserProfile>('/users/@me', { signal: controller.signal }),
      api<UserSettings>('/users/@me/settings', { signal: controller.signal }),
      loadAuthConfiguration(controller.signal)
    ])
      .then(([loadedProfile, loadedSettings, authConfiguration]) => {
        if (controller.signal.aborted || generation !== lifecycle) return;
        profile = loadedProfile;
        displayName = loadedProfile.display_name ?? '';
        bio = loadedProfile.bio ?? '';
        customStatus = loadedProfile.custom_status ?? '';
        settings = loadedSettings;
        developerModeDraft = developerModeFromSettings(loadedSettings.notification_settings);
        developerMode.apply(loadedSettings.notification_settings);
        browserNotificationsDraft = browserNotificationsFromSettings(
          loadedSettings.notification_settings
        );
        browserNotifications.apply(loadedSettings.notification_settings);
        savedTheme = loadedSettings.theme;
        emailEnabled = authConfiguration.password_recovery_enabled;
        loaded = true;
        applyTheme(settings.theme);
        applyLocale(settings.locale);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation !== lifecycle) return;
        error = userErrorMessage(caught, 'Could not load settings. Try again.');
      });
    return () => {
      lifecycle += 1;
      controller.abort();
      if (routeController === controller) routeController = null;
    };
  });

  function cancelableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolveDelay, rejectDelay) => {
      if (signal.aborted) {
        rejectDelay(new DOMException('Operation cancelled', 'AbortError'));
        return;
      }
      const timeout = window.setTimeout(finish, milliseconds);
      function finish() {
        signal.removeEventListener('abort', cancel);
        resolveDelay();
      }
      function cancel() {
        window.clearTimeout(timeout);
        rejectDelay(new DOMException('Operation cancelled', 'AbortError'));
      }
      signal.addEventListener('abort', cancel, { once: true });
    });
  }

  function beginAction() {
    error = '';
    notice = '';
    busy = true;
  }

  function actionError(caught: unknown, fallback: string) {
    const actionableFallback = /(?:try again|reload|choose|check|contact|sign in)/i.test(fallback)
      ? fallback
      : `${fallback.replace(/\.$/, '')}. Try again.`;
    error = userErrorMessage(caught, actionableFallback);
  }

  async function savePreferences() {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({
          locale: settings.locale,
          theme: settings.theme,
          dm_privacy: settings.dm_privacy
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = updated;
      savedTheme = updated.theme;
      applyTheme(settings.theme);
      applyLocale(settings.locale);
      developerMode.apply(updated.notification_settings);
      notice = 'Preferences saved.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not save preferences.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function saveProfile() {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    beginAction();
    try {
      const updated = await api<UserProfile>('/users/@me', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({
          display_name: displayName,
          bio,
          custom_status: customStatus
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      profile = updated;
      displayName = updated.display_name ?? '';
      bio = updated.bio ?? '';
      customStatus = updated.custom_status ?? '';
      notice = 'Public profile saved.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not save your public profile.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function changeTheme(theme: UserSettings['theme']) {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    const previousTheme = savedTheme;
    const draftLocale = settings.locale;
    const draftPrivacy = settings.dm_privacy;
    applyTheme(theme);
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({ theme })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = {
        ...updated,
        locale: draftLocale,
        dm_privacy: draftPrivacy
      };
      savedTheme = updated.theme;
      applyTheme(updated.theme);
      notice = 'Theme updated.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings.theme = previousTheme;
      applyTheme(previousTheme);
      actionError(caught, 'Could not update the theme.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function changeDeveloperMode(enabled: boolean) {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    const previous = developerModeDraft;
    const draftLocale = settings.locale;
    const draftTheme = settings.theme;
    const draftPrivacy = settings.dm_privacy;
    developerModeDraft = enabled;
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({
          notification_settings: {
            ...settings.notification_settings,
            developer_mode: enabled
          }
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = {
        ...updated,
        locale: draftLocale,
        theme: draftTheme,
        dm_privacy: draftPrivacy
      };
      developerModeDraft = developerModeFromSettings(updated.notification_settings);
      developerMode.apply(updated.notification_settings);
      notice = `Developer mode ${developerModeDraft ? 'enabled' : 'disabled'}.`;
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      developerModeDraft = previous;
      actionError(caught, 'Could not update developer mode.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function changeBrowserNotifications(enabled: boolean) {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    const previous = browserNotificationsDraft;
    const draftLocale = settings.locale;
    const draftTheme = settings.theme;
    const draftPrivacy = settings.dm_privacy;

    if (enabled) {
      const permission = await browserNotifications.requestPermission();
      if (controller.signal.aborted || generation !== lifecycle) return;
      if (permission !== 'granted') {
        browserNotificationsDraft = false;
        error =
          browserNotifications.permissionError ||
          (isNativeDesktop()
            ? 'Desktop notifications could not be enabled. Check system notification settings and try again.'
            : browserNotifications.supported
              ? 'Browser notifications are blocked. Allow them in this site’s browser settings and try again.'
              : 'This browser does not support notifications.');
        return;
      }
    }

    browserNotificationsDraft = enabled;
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({
          notification_settings: {
            ...settings.notification_settings,
            browser_notifications: enabled
          }
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = {
        ...updated,
        locale: draftLocale,
        theme: draftTheme,
        dm_privacy: draftPrivacy
      };
      browserNotificationsDraft = browserNotificationsFromSettings(updated.notification_settings);
      browserNotifications.apply(updated.notification_settings);
      browserNotifications.markPromptHandled();
      notice = `${isNativeDesktop() ? 'Desktop' : 'Browser'} notifications ${browserNotificationsDraft ? 'enabled' : 'disabled'}.`;
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      browserNotificationsDraft = previous;
      browserNotifications.apply(settings.notification_settings);
      actionError(
        caught,
        `Could not update ${isNativeDesktop() ? 'desktop' : 'browser'} notifications.`
      );
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function testDesktopNotification() {
    if (!isNativeDesktop() || testingNotification) return;
    error = '';
    notice = '';
    testingNotification = true;
    try {
      await nativeInvoke('native_notify', {
        title: 'Kaede Chat notifications',
        body: 'Desktop notifications are working.',
        sensitive: false,
        deepLink: null
      });
      notice =
        'Test notification sent. If it did not appear, check Windows notification settings and Do Not Disturb or Focus Assist.';
    } catch (caught) {
      error = userErrorMessage(
        nativeError(caught),
        'Could not send the test desktop notification. Check system notification settings and try again.'
      );
    } finally {
      testingNotification = false;
    }
  }

  async function requestEmailChange() {
    const controller = routeController;
    if (busy || !loaded || !controller || !nextEmail.trim() || !emailPassword) return;
    const generation = lifecycle;
    beginAction();
    try {
      await api('/auth/email/change', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ email: nextEmail.trim(), password: emailPassword })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      emailPassword = '';
      notice = `A confirmation link was sent to ${nextEmail.trim()}.`;
      nextEmail = '';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not request the email change.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function startMfaSetup() {
    const controller = routeController;
    if (busy || !loaded || !controller || !mfaPassword) return;
    const generation = lifecycle;
    beginAction();
    try {
      const setup = await api<MfaSetup>('/auth/mfa/setup', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          password: mfaPassword,
          current_code: mfaCurrentCode || null
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      mfaSetup = setup;
      mfaPassword = '';
      mfaCurrentCode = '';
      notice = 'Authenticator secret created. Verify a code to finish.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not begin authenticator setup.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function enableMfa() {
    const controller = routeController;
    if (busy || !loaded || !controller || !mfaSetup || !mfaCode) return;
    const generation = lifecycle;
    beginAction();
    try {
      const result = await api<{ status: string; recovery_codes: string[] }>('/auth/mfa/enable', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ code: mfaCode })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      recoveryCodes = result.recovery_codes;
      mfaSetup = null;
      mfaCode = '';
      if (profile) profile = { ...profile, mfa_enabled: true };
      notice = 'Two-factor authentication is enabled.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not verify the authenticator code.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function disableMfa() {
    const controller = routeController;
    if (busy || !loaded || !controller || !disablePassword || !disableCode) return;
    const generation = lifecycle;
    beginAction();
    try {
      await api('/auth/mfa/disable', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ password: disablePassword, code: disableCode })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      disablePassword = '';
      disableCode = '';
      recoveryCodes = [];
      if (profile) profile = { ...profile, mfa_enabled: false };
      notice = 'Two-factor authentication is disabled.';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not disable two-factor authentication.');
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function copyValue(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      notice = `${label} copied.`;
      error = '';
    } catch {
      error = 'Browser denied clipboard access. Allow clipboard permission and try again.';
    }
  }

  async function logout() {
    const controller = routeController;
    if (busy || !controller) return;
    const generation = lifecycle;
    beginAction();
    try {
      await api('/auth/logout', { method: 'POST', signal: controller.signal });
      if (controller.signal.aborted || generation !== lifecycle) return;
      expireBrowserSession();
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not sign out.');
      busy = false;
    }
  }

  async function uploadAsset(kind: 'avatar' | 'banner', file: File) {
    const controller = routeController;
    if (busy || !loaded || !controller || !file.type.startsWith('image/')) return;
    const generation = lifecycle;
    beginAction();
    assetProgress = 0;
    notice = `Uploading ${kind}…`;
    try {
      const ticket = await api<UploadTicket>(`/users/@me/assets/${kind}`, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ filename: file.name, content_type: file.type, size: file.size })
      });
      await uploadObject(
        ticket,
        file,
        (progress) => {
          if (generation === lifecycle) assetProgress = progress;
        },
        controller.signal
      );
      await api(`/users/@me/assets/${kind}`, {
        method: 'PUT',
        signal: controller.signal,
        body: JSON.stringify({ attachment_id: ticket.id })
      });
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const attachment = await api<{ scan_status: string }>(`/attachments/${ticket.id}`, {
          signal: controller.signal
        });
        if (attachment.scan_status === 'clean') {
          await api(`/users/@me/assets/${kind}`, {
            method: 'PUT',
            signal: controller.signal,
            body: JSON.stringify({ attachment_id: ticket.id })
          });
          const updatedProfile = await api<UserProfile>('/users/@me', {
            signal: controller.signal
          });
          if (controller.signal.aborted || generation !== lifecycle) return;
          profile = updatedProfile;
          notice = `${kind === 'avatar' ? 'Avatar' : 'Banner'} updated.`;
          return;
        }
        if (attachment.scan_status === 'infected' || attachment.scan_status === 'failed') {
          throw new Error('The image did not pass media processing.');
        }
        await cancelableDelay(1000, controller.signal);
      }
      throw new Error('Media processing is taking longer than expected. Try again shortly.');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, 'Could not update media.');
    } finally {
      if (generation === lifecycle) {
        busy = false;
        assetProgress = 0;
      }
    }
  }
</script>

<svelte:head><title>Settings · Kaede Chat</title></svelte:head>

<main class="settings-page">
  <aside class="settings-nav">
    <a class="settings-back" href={resolve('/home')}>
      <Icon name="arrow-left" size={18} />
      <span>Back to Kaede</span>
    </a>
    <div class="settings-account-mini">
      <span class="avatar avatar-small">
        {#if profile?.avatar_hash}
          <img src={assetUrl(profile.avatar_hash, 'thumbnail_128')} alt="" />
        {:else}
          {profile?.username.slice(0, 1).toUpperCase() ?? 'K'}
        {/if}
      </span>
      <span>
        <strong>{profile?.display_name ?? profile?.username ?? 'Loading…'}</strong>
        <small>{profile?.handle ?? 'Your account'}</small>
      </span>
    </div>
    <nav aria-label="Settings sections">
      <p>Account</p>
      <a href="#profile"><Icon name="user" size={18} />Profile</a>
      <a href="#security"><Icon name="shield" size={18} />Security</a>
      <p>Preferences</p>
      <a href="#appearance"><Icon name="palette" size={18} />Appearance</a>
      {#if isNativeDesktop()}<a href="#voice-devices"
          ><Icon name="volume" size={18} />Voice & devices</a
        >{/if}
      <a href="#notifications"><Icon name="bell" size={18} />Notifications</a>
      <a href="#privacy"><Icon name="lock" size={18} />Privacy</a>
      <a href="#advanced"><Icon name="settings" size={18} />Advanced</a>
    </nav>
    <button class="settings-signout" type="button" disabled={busy} onclick={logout}>
      <Icon name="logout" size={18} />
      Sign out
    </button>
  </aside>

  <section class="settings-content">
    <header class="settings-page-heading">
      <div>
        <p class="eyebrow">Your account</p>
        <h1>Settings</h1>
        <p>Manage how you appear, how Kaede feels, and how your account stays protected.</p>
      </div>
      <a class="icon-button settings-close" href={resolve('/home')} aria-label="Close settings">×</a
      >
    </header>

    {#if error}
      <div class="notice-banner error-banner" role="alert">{error}</div>
    {/if}
    <Toast message={notice} onDismiss={() => (notice = '')} />

    {#if !loaded}
      {#if !error}
        <div class="settings-loading" aria-label="Loading personal settings">
          <span></span><span></span><span></span>
        </div>
      {:else}
        <section class="empty-state">
          <span><Icon name="user" size={28} /></span>
          <h2>Settings are unavailable</h2>
          <p>Return to Kaede and try opening your settings again.</p>
          <a class="primary-button" href={resolve('/home')}>Return home</a>
        </section>
      {/if}
    {:else}
      <section id="profile" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="user" /></span>
          <div>
            <h2>Profile</h2>
            <p>Your public identity on this instance and across the federation.</p>
          </div>
        </div>

        <div class="profile-card">
          <div class="profile-banner">
            {#if profile?.banner_hash}
              <img src={assetUrl(profile.banner_hash, 'original')} alt="" />
            {:else}
              <span aria-hidden="true"></span>
            {/if}
          </div>
          <div class="profile-card-body">
            <span class="avatar avatar-large">
              {#if profile?.avatar_hash}
                <img src={assetUrl(profile.avatar_hash, 'thumbnail_128')} alt="Your avatar" />
              {:else}
                {profile?.username.slice(0, 1).toUpperCase() ?? 'K'}
              {/if}
            </span>
            <div class="profile-identity">
              <strong>{profile?.display_name ?? profile?.username ?? 'Loading…'}</strong>
              <span>{profile?.handle}</span>
              {#if profile?.custom_status}<em>{profile.custom_status}</em>{/if}
              {#if profile?.bio}<p>{profile.bio}</p>{/if}
            </div>
          </div>
        </div>

        <form
          class="settings-card settings-form profile-fields"
          onsubmit={(event) => {
            event.preventDefault();
            void saveProfile();
          }}
        >
          <div class="two-column-fields">
            <label class="form-field compact-field">
              <span>Display name</span>
              <small>Your username and permanent handle do not change.</small>
              <input bind:value={displayName} maxlength="100" disabled={busy} />
            </label>
            <label class="form-field compact-field">
              <span>Custom status</span>
              <small>Shown beneath your name in member and friend lists.</small>
              <input
                bind:value={customStatus}
                maxlength="128"
                placeholder="What are you up to?"
                disabled={busy}
              />
            </label>
          </div>
          <label class="form-field compact-field">
            <span>About me</span>
            <small>A short public description shown on your profile.</small>
            <textarea bind:value={bio} maxlength="500" rows="4" disabled={busy}></textarea>
          </label>
          <div class="profile-field-footer">
            <span>{bio.length}/500</span>
            <button class="primary-button" disabled={busy}>
              {busy ? 'Saving…' : 'Save profile'}
            </button>
          </div>
        </form>

        <div class="settings-card">
          <div class="settings-card-row">
            <div>
              <strong>Profile images</strong>
              <p>PNG, JPEG, GIF, or WebP. Files are scanned before they become public.</p>
            </div>
            <div class="profile-media-actions">
              <label class="secondary-button">
                <Icon name="user" size={16} />Change avatar
                <input
                  class="visually-hidden"
                  type="file"
                  accept="image/png,image/jpeg,image/gif,image/webp"
                  disabled={busy}
                  onchange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    if (file) void uploadAsset('avatar', file);
                    event.currentTarget.value = '';
                  }}
                />
              </label>
              <label class="secondary-button">
                <Icon name="image" size={16} />Change banner
                <input
                  class="visually-hidden"
                  type="file"
                  accept="image/png,image/jpeg,image/gif,image/webp"
                  disabled={busy}
                  onchange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    if (file) void uploadAsset('banner', file);
                    event.currentTarget.value = '';
                  }}
                />
              </label>
            </div>
          </div>
          {#if busy && assetProgress}
            <div class="upload-progress">
              <progress
                max="100"
                value={assetProgress}
                aria-label={`Profile image upload: ${assetProgress}%`}
              ></progress>
              <span>{assetProgress}%</span>
            </div>
          {/if}
        </div>
      </section>

      <section id="appearance" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="palette" /></span>
          <div>
            <h2>Appearance</h2>
            <p>Choose a theme that is comfortable wherever you chat.</p>
          </div>
        </div>
        <form
          class="settings-card settings-form"
          onsubmit={(event) => {
            event.preventDefault();
            void savePreferences();
          }}
        >
          <fieldset class="theme-picker">
            <legend>Theme</legend>
            <label>
              <input
                type="radio"
                bind:group={settings.theme}
                value="system"
                disabled={busy}
                onchange={(event) =>
                  void changeTheme(event.currentTarget.value as UserSettings['theme'])}
              />
              <span class="theme-preview system-preview"><i></i><i></i></span>
              <strong>System</strong>
              <small>Match this device</small>
            </label>
            <label>
              <input
                type="radio"
                bind:group={settings.theme}
                value="light"
                disabled={busy}
                onchange={(event) =>
                  void changeTheme(event.currentTarget.value as UserSettings['theme'])}
              />
              <span class="theme-preview light-preview"><i></i><i></i></span>
              <strong>Light</strong>
              <small>Bright and calm</small>
            </label>
            <label>
              <input
                type="radio"
                bind:group={settings.theme}
                value="dark"
                disabled={busy}
                onchange={(event) =>
                  void changeTheme(event.currentTarget.value as UserSettings['theme'])}
              />
              <span class="theme-preview dark-preview"><i></i><i></i></span>
              <strong>Dark</strong>
              <small>Easy on the eyes</small>
            </label>
          </fieldset>
          <label class="form-field">
            <span>Language</span>
            <small>Used for dates. Full interface translation is still being completed.</small>
            <select bind:value={settings.locale} disabled={busy}>
              <option value="en-US">English (United States)</option>
              <option value="ja-JP">日本語</option>
            </select>
          </label>
          <div class="form-actions">
            <button class="primary-button" disabled={busy}>
              {busy ? 'Saving…' : 'Save preferences'}
            </button>
          </div>
        </form>
      </section>

      <NativeVoiceSettings />

      <section id="notifications" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="bell" /></span>
          <div>
            <h2>Notifications</h2>
            <p>Choose when Kaede may get your attention outside the active tab.</p>
          </div>
        </div>
        <div class="settings-card">
          <div class="toggle-list">
            <label class="toggle-row">
              <span>
                <strong
                  >{isNativeDesktop() ? 'Desktop notifications' : 'Browser notifications'}</strong
                >
                <small>
                  {#if isNativeDesktop()}
                    Show operating-system notifications for direct messages and guild alerts while
                    Kaede is minimized or running in the background. Do Not Disturb suppresses them.
                  {:else}
                    Notify you about direct messages and messages allowed by each guild’s
                    notification setting while Kaede is in the background. Your browser will ask for
                    permission before this is enabled.
                  {/if}
                </small>
              </span>
              <input
                type="checkbox"
                checked={browserNotificationsDraft}
                disabled={busy || !browserNotifications.supported}
                onchange={(event) => void changeBrowserNotifications(event.currentTarget.checked)}
              />
            </label>
          </div>
          {#if !browserNotifications.supported}
            <p class="settings-helper">This browser does not support system notifications.</p>
          {:else if !isNativeDesktop() && browserNotifications.permission === 'denied'}
            <p class="settings-helper">
              Notifications are blocked in your browser. Allow them in this site’s permissions to
              turn them on.
            </p>
          {/if}
          {#if isNativeDesktop()}
            <div class="form-actions">
              <button
                type="button"
                class="secondary-button"
                disabled={testingNotification || !browserNotificationsDraft}
                onclick={() => void testDesktopNotification()}
              >
                {testingNotification ? 'Sending…' : 'Send test notification'}
              </button>
            </div>
            <p class="settings-helper">
              Regular message notifications are intentionally quiet while Kaede is focused. Use this
              test to check Windows delivery without minimizing the app.
            </p>
          {/if}
        </div>
      </section>

      <section id="privacy" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="lock" /></span>
          <div>
            <h2>Privacy</h2>
            <p>Control who is allowed to start a direct conversation with you.</p>
          </div>
        </div>
        <form
          class="settings-card settings-form"
          onsubmit={(event) => {
            event.preventDefault();
            void savePreferences();
          }}
        >
          <label class="form-field">
            <span>Direct messages</span>
            <small>
              This rule is enforced by the server where your account lives (your home instance),
              including federated requests.
            </small>
            <select bind:value={settings.dm_privacy} disabled={busy}>
              <option value="everyone">Anyone on a known instance</option>
              <option value="shared_guild">Friends and people who share a guild with me</option>
              <option value="friends">Friends only</option>
            </select>
          </label>
          <div class="form-actions">
            <button class="primary-button" disabled={busy}>
              {busy ? 'Saving…' : 'Save privacy'}
            </button>
          </div>
        </form>
      </section>

      <section id="advanced" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="settings" /></span>
          <div>
            <h2>Advanced</h2>
            <p>Optional tools for development, integrations, and troubleshooting.</p>
          </div>
        </div>
        <div class="settings-card">
          <div class="toggle-list">
            <label class="toggle-row">
              <span>
                <strong>Developer mode</strong>
                <small>
                  Show technical user, channel, and message IDs in context menus and profiles.
                </small>
              </span>
              <input
                type="checkbox"
                checked={developerModeDraft}
                disabled={busy}
                onchange={(event) => void changeDeveloperMode(event.currentTarget.checked)}
              />
            </label>
          </div>
        </div>
      </section>

      <section id="security" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="shield" /></span>
          <div>
            <h2>Security</h2>
            <p>Protect access to your account and recovery details.</p>
          </div>
        </div>

        <div class="settings-card security-card">
          <div class="settings-card-row">
            <div class="security-label">
              <span class="status-dot" class:enabled={profile?.mfa_enabled}></span>
              <div>
                <strong>Two-factor authentication</strong>
                <p>
                  {profile?.mfa_enabled
                    ? 'An authenticator is required when you sign in.'
                    : 'Add an authenticator app and one-time recovery codes.'}
                </p>
              </div>
            </div>
            <span class:positive-chip={profile?.mfa_enabled} class="status-chip">
              {profile?.mfa_enabled ? 'Enabled' : 'Not enabled'}
            </span>
          </div>

          {#if recoveryCodes.length}
            <div class="security-flow recovery-panel">
              <div>
                <strong>Save these recovery codes now</strong>
                <p>Each code works once. They will not be shown again after you leave this page.</p>
              </div>
              <div class="recovery-grid">
                {#each recoveryCodes as code (code)}<code>{code}</code>{/each}
              </div>
              <button
                class="secondary-button"
                type="button"
                onclick={() => copyValue(recoveryCodes.join('\n'), 'Recovery codes')}
              >
                <Icon name="copy" size={16} />Copy all codes
              </button>
            </div>
          {:else if mfaSetup}
            <form
              class="security-flow"
              onsubmit={(event) => {
                event.preventDefault();
                void enableMfa();
              }}
            >
              <div>
                <strong>Connect your authenticator</strong>
                <p>Enter this secret manually, then verify the six-digit code it generates.</p>
              </div>
              <div class="secret-value">
                <code>{mfaSetup.secret}</code>
                <button
                  class="icon-button"
                  type="button"
                  aria-label="Copy authenticator secret"
                  onclick={() => copyValue(mfaSetup?.secret ?? '', 'Authenticator secret')}
                >
                  <Icon name="copy" size={17} />
                </button>
              </div>
              <label class="form-field compact-field">
                <span>Verification code</span>
                <input
                  bind:value={mfaCode}
                  inputmode="numeric"
                  autocomplete="one-time-code"
                  minlength="6"
                  maxlength="32"
                  required
                />
              </label>
              <div class="form-actions">
                <button
                  class="secondary-button"
                  type="button"
                  onclick={() => {
                    mfaSetup = null;
                    mfaCode = '';
                  }}>Cancel</button
                >
                <button class="primary-button" disabled={busy}>Enable authenticator</button>
              </div>
            </form>
          {:else if profile?.mfa_enabled}
            <form
              class="security-flow"
              onsubmit={(event) => {
                event.preventDefault();
                void startMfaSetup();
              }}
            >
              <div>
                <strong>Replace your authenticator</strong>
                <p>Confirm your password and current factor before connecting a new app.</p>
              </div>
              <div class="two-column-fields">
                <label class="form-field compact-field">
                  <span>Password</span>
                  <input
                    bind:value={mfaPassword}
                    type="password"
                    autocomplete="current-password"
                    maxlength="256"
                    required
                  />
                </label>
                <label class="form-field compact-field">
                  <span>Current authenticator or recovery code</span>
                  <input
                    bind:value={mfaCurrentCode}
                    autocomplete="one-time-code"
                    minlength="6"
                    maxlength="32"
                    required
                  />
                </label>
              </div>
              <div class="form-actions">
                <button class="secondary-button" disabled={busy}>
                  <Icon name="key" size={16} />Replace authenticator
                </button>
              </div>
            </form>
            <form
              class="security-flow"
              onsubmit={(event) => {
                event.preventDefault();
                void disableMfa();
              }}
            >
              <div>
                <strong>Disable two-factor authentication</strong>
                <p>This requires your password and a current authenticator or recovery code.</p>
              </div>
              <div class="two-column-fields">
                <label class="form-field compact-field">
                  <span>Password</span>
                  <input
                    bind:value={disablePassword}
                    type="password"
                    autocomplete="current-password"
                    maxlength="256"
                    required
                  />
                </label>
                <label class="form-field compact-field">
                  <span>Authenticator code</span>
                  <input
                    bind:value={disableCode}
                    autocomplete="one-time-code"
                    minlength="6"
                    maxlength="32"
                    required
                  />
                </label>
              </div>
              <div class="form-actions">
                <button class="danger-button" disabled={busy}
                  >Disable two-factor authentication</button
                >
              </div>
            </form>
          {:else}
            <form
              class="security-flow"
              onsubmit={(event) => {
                event.preventDefault();
                void startMfaSetup();
              }}
            >
              <label class="form-field compact-field">
                <span>Confirm your password</span>
                <input
                  bind:value={mfaPassword}
                  type="password"
                  autocomplete="current-password"
                  maxlength="256"
                  required
                />
              </label>
              <div class="form-actions">
                <button class="primary-button" disabled={busy}>
                  <Icon name="key" size={16} />Set up authenticator
                </button>
              </div>
            </form>
          {/if}
        </div>

        {#if emailEnabled}
          <form
            class="settings-card settings-form"
            onsubmit={(event) => {
              event.preventDefault();
              void requestEmailChange();
            }}
          >
            <div class="settings-card-row">
              <div>
                <strong>Email address</strong>
                <p>
                  {profile?.email ?? 'No email address'}
                  {#if profile?.email_verified}
                    <span class="verified-label"><Icon name="check" size={13} />Verified</span>
                  {/if}
                </p>
              </div>
              <Icon name="mail" />
            </div>
            <div class="two-column-fields">
              <label class="form-field compact-field">
                <span>New email</span>
                <input
                  bind:value={nextEmail}
                  type="email"
                  autocomplete="email"
                  maxlength="320"
                  required
                />
              </label>
              <label class="form-field compact-field">
                <span>Current password</span>
                <input
                  bind:value={emailPassword}
                  type="password"
                  autocomplete="current-password"
                  maxlength="256"
                  required
                />
              </label>
            </div>
            <div class="form-actions">
              <button class="secondary-button" disabled={busy}>Send confirmation</button>
            </div>
          </form>
        {:else if emailEnabled === false}
          <div class="settings-card settings-card-row">
            <div>
              <strong>Email-free account</strong>
              <p>This instance does not require or deliver email.</p>
            </div>
            <Icon name="mail" />
          </div>
        {/if}
      </section>
    {/if}

    <footer class="settings-footer">
      <span>Kaede Chat</span>
      <span>Your handle never changes: {profile?.handle ?? '—'}</span>
    </footer>
  </section>
</main>
