<script lang="ts">
  import { cancelableDelay } from '$lib/ui/delay';
  import { resolve } from '$app/paths';
  import { api, expireBrowserSession, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { loadPasswordKdfContext, preparePassword } from '$lib/auth/password-kdf';
  import type { UserSummary } from '$lib/chat/types';
  import Icon from '$lib/components/Icon.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import NativeVoiceSettings from '$lib/components/NativeVoiceSettings.svelte';
  import NativeDesktopSettings from '$lib/components/NativeDesktopSettings.svelte';
  import E2EESettings from '$lib/components/E2EESettings.svelte';
  import UserApplicationInstallations from '$lib/components/UserApplicationInstallations.svelte';
  import { clearActiveE2EEState } from '$lib/e2ee/client';
  import { isNativeDesktop, nativeError, nativeInvoke } from '$lib/platform/native';
  import { assetUrl } from '$lib/media/assets';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import {
    browserNotifications,
    browserNotificationsFromSettings
  } from '$lib/notifications/browser.svelte';
  import { developerMode, developerModeFromSettings } from '$lib/ui/developer-mode.svelte';
  import {
    applyTtsPreferences,
    ttsPreferencesFromSettings,
    type TtsPlaybackMode,
    type TtsPreferences
  } from '$lib/chat/tts';
  import {
    applyLocale,
    languages,
    localePreference,
    markLanguageChosen,
    matchLanguage,
    t
  } from '$lib/ui/locale';
  import { applyTheme, type ThemePreference } from '$lib/ui/theme';
  import { loadMediaQuality, saveMediaQuality } from '$lib/voice/quality';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { onMount } from 'svelte';

  interface UserProfile extends UserSummary {
    banner_hash: string | null;
    bio: string | null;
    custom_status: string | null;
    email: string | null;
    email_verified: boolean;
    mfa_enabled: boolean;
    age_assurance_state: 'unknown' | 'adult' | 'minor';
  }

  interface UserSettings {
    locale: string;
    theme: ThemePreference;
    dm_privacy: 'everyone' | 'shared_guild' | 'friends';
    share_locale_with_bots: boolean;
    age_restricted_dm_commands_enabled: boolean;
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
    share_locale_with_bots: true,
    age_restricted_dm_commands_enabled: false,
    notification_settings: {}
  });
  let emailEnabled = $state<boolean | null>(null);
  let notice = $state('');
  let error = $state('');
  let loaded = $state(false);
  let administrationAvailable = $state(false);
  let busy = $state(false);
  let savedTheme = $state<UserSettings['theme']>('system');
  let assetProgress = $state(0);
  let assetStage = $state<'uploading' | 'processing' | null>(null);
  let lifecycle = 0;
  let routeController: AbortController | null = null;
  let displayName = $state('');
  let bio = $state('');
  let customStatus = $state('');
  let developerModeDraft = $state(false);
  let opusDtxDraft = $state(true);
  let browserNotificationsDraft = $state(false);
  let testingNotification = $state(false);
  let ttsEnabledDraft = $state(false);
  let ttsPlaybackDraft = $state<TtsPlaybackMode>('never');
  let ttsRateDraft = $state(1);

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
    opusDtxDraft = loadMediaQuality().dtx;
    void api('/administration/@me')
      .then(() => (administrationAvailable = true))
      .catch(() => (administrationAvailable = false));
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
        settings = {
          ...loadedSettings,
          locale:
            loadedSettings.locale === 'system'
              ? 'system'
              : (matchLanguage(loadedSettings.locale) ?? 'en')
        };
        developerModeDraft = developerModeFromSettings(loadedSettings.notification_settings);
        developerMode.apply(loadedSettings.notification_settings);
        browserNotificationsDraft = browserNotificationsFromSettings(
          loadedSettings.notification_settings
        );
        browserNotifications.apply(loadedSettings.notification_settings);
        const tts = ttsPreferencesFromSettings(loadedSettings.notification_settings);
        ttsEnabledDraft = tts.enabled;
        ttsPlaybackDraft = tts.playback;
        ttsRateDraft = tts.rate;
        applyTtsPreferences(tts);
        savedTheme = loadedSettings.theme;
        emailEnabled = authConfiguration.password_recovery_enabled;
        loaded = true;
        applyTheme(settings.theme);
        applyLocale(settings.locale);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation !== lifecycle) return;
        error = userErrorMessage(caught, $t('ui_could_not_load_settings_try_again_e5375e07'));
      });
    return () => {
      lifecycle += 1;
      controller.abort();
      if (routeController === controller) routeController = null;
    };
  });

  async function changeLanguage(locale: string) {
    if (busy) return;
    const previous = $localePreference;
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: routeController?.signal,
        body: JSON.stringify({ locale })
      });
      if (routeController?.signal.aborted) return;
      settings.locale =
        updated.locale === 'system' ? 'system' : (matchLanguage(updated.locale) ?? 'en');
      applyLocale(updated.locale);
      markLanguageChosen();
    } catch {
      settings.locale = previous === 'system' ? 'system' : (matchLanguage(previous) ?? 'en');
      error = $t('language_save_error');
    } finally {
      busy = false;
    }
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

  async function currentPasswordPayload(password: string) {
    if (!profile) throw new Error($t('ui_your_account_details_are_not_loaded_reload_an_95a79c4a'));
    const prepared = await preparePassword(password, await loadPasswordKdfContext(profile.handle));
    return {
      password: prepared.authenticationSecret,
      password_kdf_version: prepared.context.version
    };
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
          dm_privacy: settings.dm_privacy,
          share_locale_with_bots: settings.share_locale_with_bots,
          age_restricted_dm_commands_enabled: settings.age_restricted_dm_commands_enabled
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = updated;
      savedTheme = updated.theme;
      applyTheme(settings.theme);
      applyLocale(settings.locale);
      developerMode.apply(updated.notification_settings);
      notice = $t('ui_preferences_saved_60d6766a');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_save_preferences_cc5fc9bd'));
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
      notice = $t('ui_public_profile_saved_c239a47e');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_save_your_public_profile_270836b0'));
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
    const draftShareLocale = settings.share_locale_with_bots;
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
        dm_privacy: draftPrivacy,
        share_locale_with_bots: draftShareLocale
      };
      savedTheme = updated.theme;
      applyTheme(updated.theme);
      notice = $t('ui_theme_updated_419ead52');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings.theme = previousTheme;
      applyTheme(previousTheme);
      actionError(caught, $t('ui_could_not_update_the_theme_5199cfaa'));
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
    const draftShareLocale = settings.share_locale_with_bots;
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
        dm_privacy: draftPrivacy,
        share_locale_with_bots: draftShareLocale
      };
      developerModeDraft = developerModeFromSettings(updated.notification_settings);
      developerMode.apply(updated.notification_settings);
      notice = `Developer mode ${developerModeDraft ? 'enabled' : 'disabled'}.`;
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      developerModeDraft = previous;
      actionError(caught, $t('ui_could_not_update_developer_mode_a98923a5'));
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  function changeOpusDtx(enabled: boolean) {
    const preferences = loadMediaQuality();
    saveMediaQuality({ ...preferences, dtx: enabled });
    opusDtxDraft = enabled;
    notice = `Opus DTX ${enabled ? 'enabled' : 'disabled'} for future microphone publications.`;
  }

  async function changeBrowserNotifications(enabled: boolean) {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    const previous = browserNotificationsDraft;
    const draftLocale = settings.locale;
    const draftTheme = settings.theme;
    const draftPrivacy = settings.dm_privacy;
    const draftShareLocale = settings.share_locale_with_bots;

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
        dm_privacy: draftPrivacy,
        share_locale_with_bots: draftShareLocale
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
        title: $t('ui_kaede_chat_notifications_21fec172'),
        body: 'Desktop notifications are working.',
        sensitive: false,
        deepLink: null
      });
      notice = $t('ui_test_notification_sent_if_it_did_not_appear_c_8fec766a');
    } catch (caught) {
      error = userErrorMessage(
        nativeError(caught),
        $t('ui_could_not_send_the_test_desktop_notification__51c8026a')
      );
    } finally {
      testingNotification = false;
    }
  }

  async function changeTtsPreferences(patch: Partial<TtsPreferences>) {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const generation = lifecycle;
    const previous = ttsPreferencesFromSettings(settings.notification_settings);
    const next: TtsPreferences = {
      enabled: patch.enabled ?? ttsEnabledDraft,
      playback: patch.playback ?? ttsPlaybackDraft,
      rate: patch.rate ?? ttsRateDraft
    };
    ttsEnabledDraft = next.enabled;
    ttsPlaybackDraft = next.playback;
    ttsRateDraft = next.rate;
    beginAction();
    try {
      const updated = await api<UserSettings>('/users/@me/settings', {
        method: 'PATCH',
        signal: controller.signal,
        body: JSON.stringify({
          notification_settings: {
            ...settings.notification_settings,
            tts_enabled: next.enabled,
            tts_playback: next.playback,
            tts_rate: next.rate
          }
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      settings = updated;
      const saved = ttsPreferencesFromSettings(updated.notification_settings);
      ttsEnabledDraft = saved.enabled;
      ttsPlaybackDraft = saved.playback;
      ttsRateDraft = saved.rate;
      applyTtsPreferences(saved);
      notice = $t('ui_text_to_speech_preferences_updated_ef533997');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      ttsEnabledDraft = previous.enabled;
      ttsPlaybackDraft = previous.playback;
      ttsRateDraft = previous.rate;
      applyTtsPreferences(previous);
      actionError(caught, $t('ui_could_not_update_text_to_speech_preferences_6aaf3f04'));
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }

  async function requestEmailChange() {
    const controller = routeController;
    if (busy || !loaded || !controller || !nextEmail.trim() || !emailPassword) return;
    const generation = lifecycle;
    beginAction();
    try {
      const passwordPayload = await currentPasswordPayload(emailPassword);
      await api('/auth/email/change', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ email: nextEmail.trim(), ...passwordPayload })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      emailPassword = '';
      notice = `A confirmation link was sent to ${nextEmail.trim()}.`;
      nextEmail = '';
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_request_the_email_change_95ebaa70'));
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
      const passwordPayload = await currentPasswordPayload(mfaPassword);
      const setup = await api<MfaSetup>('/auth/mfa/setup', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          ...passwordPayload,
          current_code: mfaCurrentCode || null
        })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      mfaSetup = setup;
      mfaPassword = '';
      mfaCurrentCode = '';
      notice = $t('ui_authenticator_secret_created_verify_a_code_to_30f46782');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_begin_authenticator_setup_4c91dcc4'));
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
      notice = $t('ui_two_factor_authentication_is_enabled_9f98ab30');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_verify_the_authenticator_code_e1e9ed8f'));
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
      const passwordPayload = await currentPasswordPayload(disablePassword);
      await api('/auth/mfa/disable', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({ ...passwordPayload, code: disableCode })
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      disablePassword = '';
      disableCode = '';
      recoveryCodes = [];
      if (profile) profile = { ...profile, mfa_enabled: false };
      notice = $t('ui_two_factor_authentication_is_disabled_360cf0b6');
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_disable_two_factor_authentication_7222da07'));
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
      error = $t('ui_browser_denied_clipboard_access_allow_clipboa_1319db32');
    }
  }

  async function logout() {
    const controller = routeController;
    if (busy || !controller) return;
    beginAction();
    try {
      try {
        await api('/auth/logout', { method: 'POST', signal: controller.signal });
      } catch {
        // Explicit logout is locally authoritative. The server session expires
        // independently, while local credentials and encryption state must be
        // removed even when the instance is offline.
      }
    } finally {
      try {
        await clearActiveE2EEState();
      } finally {
        expireBrowserSession();
      }
    }
  }

  async function uploadAsset(kind: 'avatar' | 'banner', file: File) {
    const controller = routeController;
    if (busy || !loaded || !controller || !file.type.startsWith('image/')) return;
    const generation = lifecycle;
    beginAction();
    assetProgress = 0;
    assetStage = 'uploading';
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
      assetProgress = 100;
      assetStage = 'processing';
      notice = `Scanning and optimizing ${kind}…`;
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
        if (
          attachment.scan_status === 'rejected' ||
          attachment.scan_status === 'infected' ||
          attachment.scan_status === 'failed'
        ) {
          throw new Error($t('ui_the_image_did_not_pass_media_processing_31c2350e'));
        }
        await cancelableDelay(1000, controller.signal);
      }
      throw new Error($t('ui_media_processing_is_taking_longer_than_expect_44788698'));
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, $t('ui_could_not_update_media_94de5bae'));
    } finally {
      if (generation === lifecycle) {
        busy = false;
        assetProgress = 0;
        assetStage = null;
      }
    }
  }

  async function removeAsset(kind: 'avatar' | 'banner') {
    const controller = routeController;
    if (busy || !loaded || !controller) return;
    const label = kind === 'avatar' ? 'avatar' : 'banner';
    if (!window.confirm(`Remove your ${label}? You can upload a new one at any time.`)) return;
    const generation = lifecycle;
    beginAction();
    try {
      const updated = await api<UserProfile>(`/users/@me/assets/${kind}`, {
        method: 'DELETE',
        signal: controller.signal
      });
      if (controller.signal.aborted || generation !== lifecycle) return;
      profile = updated;
      chatEntities.applyUserProfile(updated);
      notice = `${kind === 'avatar' ? 'Avatar' : 'Banner'} removed.`;
    } catch (caught) {
      if (controller.signal.aborted || generation !== lifecycle) return;
      actionError(caught, `Could not remove your ${label}.`);
    } finally {
      if (generation === lifecycle) busy = false;
    }
  }
</script>

<svelte:head><title>{$t('ui_settings_kaede_chat_80139e41')}</title></svelte:head>

<main class="settings-page">
  <aside class="settings-nav">
    <a class="settings-back" href={resolve('/home')}>
      <Icon name="arrow-left" size={18} />
      <span>{$t('ui_back_to_kaede_e13fc875')}</span>
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
        <strong>{profile?.display_name ?? profile?.username ?? $t('ui_loading_ba3bbbe1')}</strong>
        <small>{profile?.handle ?? $t('ui_your_account_dbb5f637')}</small>
      </span>
    </div>
    <nav aria-label={$t('ui_settings_sections_e26d51d3')}>
      <p>{$t('ui_account_7e1b0d56')}</p>
      <a href="#profile"><Icon name="user" size={18} />{$t('ui_profile_d696a35b')}</a>
      <a href="#security"><Icon name="shield" size={18} />{$t('ui_security_8f6fb4eb')}</a>
      <a href="#authorized-apps"
        ><Icon name="server" size={18} />{$t('ui_authorized_apps_c3ecd1b4')}</a
      >
      <a href={resolve('/reports')}
        ><Icon name="shield" size={18} />{$t('ui_my_reports_cc6e3f45')}</a
      >
      <p>{$t('ui_preferences_66962f72')}</p>
      <a href="#appearance"><Icon name="palette" size={18} />{$t('ui_appearance_3907fa7f')}</a>
      <a href="#accessibility"><Icon name="volume" size={18} />{$t('ui_accessibility_d3368cbf')}</a>
      {#if isNativeDesktop()}<a href="#voice-devices"
          ><Icon name="volume" size={18} />{$t('ui_voice_devices_48691508')}</a
        >{/if}
      {#if isNativeDesktop()}<a href="#desktop-app"
          ><Icon name="settings" size={18} />{$t('ui_desktop_app_71028420')}</a
        >{/if}
      <a href="#notifications"><Icon name="bell" size={18} />{$t('ui_notifications_78801183')}</a>
      <a href="#privacy"><Icon name="lock" size={18} />{$t('ui_privacy_54a57c31')}</a>
      <a href="#advanced"><Icon name="settings" size={18} />{$t('ui_advanced_9f088dbe')}</a>
      <p>{$t('ui_build_and_operate_06e611b9')}</p>
      <a href={resolve('/developers')}
        ><Icon name="server" size={18} />{$t('ui_developer_portal_1eb68022')}</a
      >
      {#if administrationAvailable}<a href={resolve('/administration')}
          ><Icon name="shield" size={18} />{$t('ui_administration_4b42c669')}</a
        >{/if}
    </nav>
    <button class="settings-signout" type="button" disabled={busy} onclick={logout}>
      <Icon name="logout" size={18} />
      {$t('ui_sign_out_48f0d3d3')}
    </button>
  </aside>

  <section class="settings-content">
    <header class="settings-page-heading">
      <div>
        <p class="eyebrow">{$t('ui_your_account_dbb5f637')}</p>
        <h1>{$t('ui_settings_74a883a0')}</h1>
        <p>{$t('ui_manage_how_you_appear_how_kaede_feels_and_how_2a5874c0')}</p>
      </div>
      <a
        class="icon-button settings-close"
        href={resolve('/home')}
        aria-label={$t('ui_close_settings_0ccfef82')}>×</a
      >
    </header>

    {#if error}
      <div class="notice-banner error-banner" role="alert">{error}</div>
    {/if}
    <Toast message={notice} onDismiss={() => (notice = '')} />

    {#if !loaded}
      {#if !error}
        <div class="settings-loading" aria-label={$t('ui_loading_personal_settings_6a01e198')}>
          <span></span><span></span><span></span>
        </div>
      {:else}
        <section class="empty-state">
          <span><Icon name="user" size={28} /></span>
          <h2>{$t('ui_settings_are_unavailable_3197fb24')}</h2>
          <p>{$t('ui_return_to_kaede_and_try_opening_your_settings_7fcbbe13')}</p>
          <a class="primary-button" href={resolve('/home')}>{$t('ui_return_home_bbcc935e')}</a>
        </section>
      {/if}
    {:else}
      <section id="profile" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="user" /></span>
          <div>
            <h2>{$t('ui_profile_d696a35b')}</h2>
            <p>{$t('ui_your_public_identity_on_this_instance_and_acr_0adfdefb')}</p>
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
                <img
                  src={assetUrl(profile.avatar_hash, 'thumbnail_128')}
                  alt={$t('ui_your_avatar_ec225b2e')}
                />
              {:else}
                {profile?.username.slice(0, 1).toUpperCase() ?? 'K'}
              {/if}
            </span>
            <div class="profile-identity">
              <strong
                >{profile?.display_name ?? profile?.username ?? $t('ui_loading_ba3bbbe1')}</strong
              >
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
              <span>{$t('ui_display_name_2b7f6a84')}</span>
              <small>{$t('ui_your_username_and_permanent_handle_do_not_cha_a700950c')}</small>
              <input bind:value={displayName} maxlength="100" disabled={busy} />
            </label>
            <label class="form-field compact-field">
              <span>{$t('ui_custom_status_ad05b1c0')}</span>
              <small>{$t('ui_shown_beneath_your_name_in_member_and_friend__d53083db')}</small>
              <input
                bind:value={customStatus}
                maxlength="128"
                placeholder={$t('ui_what_are_you_up_to_c1d70b5b')}
                disabled={busy}
              />
            </label>
          </div>
          <label class="form-field compact-field">
            <span>{$t('ui_about_me_1359ec88')}</span>
            <small>{$t('ui_a_short_public_description_shown_on_your_prof_e3dc7b50')}</small>
            <textarea bind:value={bio} maxlength="500" rows="4" disabled={busy}></textarea>
          </label>
          <div class="profile-field-footer">
            <span>{bio.length}/500</span>
            <button class="primary-button" disabled={busy}>
              {busy
                ? assetStage
                  ? $t('ui_processing_image_51bc622f')
                  : $t('ui_saving_23e39291')
                : $t('ui_save_profile_0c8209e7')}
            </button>
          </div>
        </form>

        <div class="settings-card">
          <div class="settings-card-row">
            <div>
              <strong>{$t('ui_profile_images_8193816b')}</strong>
              <p>{$t('ui_png_jpeg_gif_or_webp_files_are_scanned_before_da72aea4')}</p>
            </div>
            <div class="profile-media-actions">
              <label class="secondary-button">
                <Icon name="user" size={16} />{$t('ui_change_avatar_732392ee')}
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
              {#if profile?.avatar_hash}
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy}
                  onclick={() => void removeAsset('avatar')}
                >
                  <Icon name="trash" size={16} />{$t('ui_remove_avatar_5ae2a862')}
                </button>
              {/if}
              <label class="secondary-button">
                <Icon name="image" size={16} />{$t('ui_change_banner_6ca19843')}
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
              {#if profile?.banner_hash}
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy}
                  onclick={() => void removeAsset('banner')}
                >
                  <Icon name="trash" size={16} />{$t('ui_remove_banner_0f667465')}
                </button>
              {/if}
            </div>
          </div>
          {#if busy && assetStage}
            <div class="upload-progress" aria-live="polite">
              {#if assetStage === 'uploading'}
                <progress
                  max="100"
                  value={assetProgress}
                  aria-label={`Profile image upload: ${assetProgress}%`}
                ></progress>
                <span>{assetProgress}%</span>
              {:else}
                <progress aria-label={$t('ui_scanning_and_optimizing_profile_image_d6fefab1')}
                ></progress>
                <span>{$t('ui_processing_42074396')}</span>
              {/if}
            </div>
          {/if}
        </div>
      </section>

      <section id="appearance" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="palette" /></span>
          <div>
            <h2>{$t('ui_appearance_3907fa7f')}</h2>
            <p>{$t('ui_choose_a_theme_that_is_comfortable_wherever_y_d051415a')}</p>
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
            <legend>{$t('ui_theme_efb52e71')}</legend>
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
              <strong>{$t('ui_system_6725e7bb')}</strong>
              <small>{$t('ui_match_this_device_5ac32633')}</small>
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
              <strong>{$t('ui_light_dbcd5e7b')}</strong>
              <small>{$t('ui_bright_and_calm_c8bfa8e4')}</small>
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
              <strong>{$t('ui_dark_60acc53f')}</strong>
              <small>{$t('ui_easy_on_the_eyes_826a7461')}</small>
            </label>
          </fieldset>
          <label class="form-field">
            <span>{$t('language_settings')}</span>
            <small>{$t('language_description')}</small>
            <select
              bind:value={settings.locale}
              disabled={busy}
              onchange={(event) => void changeLanguage(event.currentTarget.value)}
            >
              <option value="system">{$t('language_system')}</option>
              {#each languages as language (language.code)}
                <option value={language.code}>{language.name}</option>
              {/each}
            </select>
            <a href="https://weblate.kaede.chat/" target="_blank" rel="noreferrer"
              >{$t('language_contribute')}</a
            >
          </label>
          <label class="settings-toggle-row">
            <span>
              <strong>{$t('ui_age_restricted_commands_in_direct_messages_be070e4c')}</strong>
              <small>
                {profile?.age_assurance_state === 'adult'
                  ? $t('ui_allow_age_restricted_application_commands_in__2421c956')
                  : profile?.age_assurance_state === 'minor'
                    ? $t('ui_unavailable_because_this_account_is_age_assur_6427553e')
                    : $t('ui_unavailable_until_your_instance_has_completed_5179d18f')}
              </small>
            </span>
            <input
              type="checkbox"
              bind:checked={settings.age_restricted_dm_commands_enabled}
              disabled={busy || profile?.age_assurance_state !== 'adult'}
            />
          </label>
          <div class="form-actions">
            <button class="primary-button" disabled={busy}>
              {busy ? $t('ui_saving_23e39291') : $t('ui_save_preferences_089e57e3')}
            </button>
          </div>
        </form>
      </section>

      <NativeVoiceSettings />

      <section id="accessibility" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="volume" /></span>
          <div>
            <h2>{$t('ui_accessibility_d3368cbf')}</h2>
            <p>{$t('ui_control_text_to_speech_playback_and_reading_s_32b9041f')}</p>
          </div>
        </div>
        <div class="settings-card settings-form">
          <label class="settings-toggle-row">
            <span>
              <strong>{$t('ui_allow_playback_and_usage_of_tts_command_34fc8872')}</strong>
              <small> {$t('ui_when_off_kaede_will_not_send_or_speak_text_to_f313f19f')} </small>
            </span>
            <input
              type="checkbox"
              checked={ttsEnabledDraft}
              disabled={busy}
              onchange={(event) =>
                void changeTtsPreferences({ enabled: event.currentTarget.checked })}
            />
          </label>
          <label class="form-field">
            <span
              >{$t('ui_text_to_speech_rate_value0_1486f1a9', {
                value0: String(ttsRateDraft.toFixed(1))
              })}</span
            >
            <input
              type="range"
              min="0.5"
              max="2"
              step="0.1"
              value={ttsRateDraft}
              disabled={busy || !ttsEnabledDraft}
              onchange={(event) =>
                void changeTtsPreferences({ rate: Number(event.currentTarget.value) })}
            />
          </label>
        </div>
      </section>

      {#if isNativeDesktop()}<NativeDesktopSettings />{/if}

      <section id="notifications" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="bell" /></span>
          <div>
            <h2>{$t('ui_notifications_78801183')}</h2>
            <p>{$t('ui_choose_when_kaede_may_get_your_attention_outs_558c1c4f')}</p>
          </div>
        </div>
        <div class="settings-card">
          <div class="toggle-list">
            <label class="toggle-row">
              <span>
                <strong
                  >{isNativeDesktop()
                    ? $t('ui_desktop_notifications_04c55b31')
                    : $t('ui_browser_notifications_7761b572')}</strong
                >
                <small>
                  {#if isNativeDesktop()}{$t(
                      'ui_show_operating_system_notifications_for_direc_a6842ff8'
                    )}{:else}{$t('ui_notify_you_about_direct_messages_and_messages_5fae53f0')}{/if}
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
            <p class="settings-helper">
              {$t('ui_this_browser_does_not_support_system_notifica_2b042d5d')}
            </p>
          {:else if !isNativeDesktop() && browserNotifications.permission === 'denied'}
            <p class="settings-helper">
              {$t('ui_notifications_are_blocked_in_your_browser_all_bfd725b6')}
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
                {testingNotification
                  ? $t('ui_sending_b8ed5279')
                  : $t('ui_send_test_notification_d0ef86e2')}
              </button>
            </div>
            <p class="settings-helper">
              {$t('ui_regular_message_notifications_are_intentional_024b521c')}
            </p>
          {/if}
          <label class="form-field">
            <span>{$t('ui_text_to_speech_06a2701c')}</span>
            <small>{$t('ui_choose_which_incoming_tts_messages_this_devic_a22ac30b')}</small>
            <select
              value={ttsPlaybackDraft}
              disabled={busy || !ttsEnabledDraft}
              onchange={(event) =>
                void changeTtsPreferences({
                  playback: event.currentTarget.value as TtsPlaybackMode
                })}
            >
              <option value="all">{$t('ui_for_all_channels_19334702')}</option>
              <option value="current">{$t('ui_for_current_selected_channel_667ad617')}</option>
              <option value="never">{$t('ui_never_6300ef80')}</option>
            </select>
          </label>
        </div>
      </section>

      <section id="privacy" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="lock" /></span>
          <div>
            <h2>{$t('ui_privacy_54a57c31')}</h2>
            <p>{$t('privacy_description')}</p>
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
            <span>{$t('ui_direct_messages_95e66705')}</span>
            <small> {$t('ui_this_rule_is_enforced_by_the_server_where_you_9c7816f6')} </small>
            <select bind:value={settings.dm_privacy} disabled={busy}>
              <option value="everyone">{$t('ui_anyone_on_a_known_instance_809b95a5')}</option>
              <option value="shared_guild"
                >{$t('ui_friends_and_people_who_share_a_guild_with_me_80479275')}</option
              >
              <option value="friends">{$t('ui_friends_only_9f75521f')}</option>
            </select>
          </label>
          <label class="toggle-row">
            <span>
              <strong>{$t('share_locale_with_bots')}</strong>
              <small>{$t('share_locale_with_bots_description')}</small>
            </span>
            <input type="checkbox" bind:checked={settings.share_locale_with_bots} disabled={busy} />
          </label>
          <div class="form-actions">
            <button class="primary-button" disabled={busy}>
              {busy ? $t('ui_saving_23e39291') : $t('ui_save_privacy_6b9197ce')}
            </button>
          </div>
        </form>
      </section>

      <UserApplicationInstallations />

      <section id="advanced" class="settings-section">
        <div class="settings-section-heading">
          <span class="section-icon"><Icon name="settings" /></span>
          <div>
            <h2>{$t('ui_advanced_9f088dbe')}</h2>
            <p>{$t('ui_optional_tools_for_development_integrations_a_52dff619')}</p>
          </div>
        </div>
        <div class="settings-card">
          <div class="toggle-list">
            {#if !isNativeDesktop()}
              <label class="toggle-row">
                <span>
                  <strong>{$t('ui_opus_discontinuous_transmission_d7a0e7e0')}</strong>
                  <small>{$t('ui_reduce_outgoing_bandwidth_while_you_are_not_s_6f63c701')}</small>
                </span>
                <input
                  type="checkbox"
                  checked={opusDtxDraft}
                  onchange={(event) => changeOpusDtx(event.currentTarget.checked)}
                />
              </label>
            {/if}
            <label class="toggle-row">
              <span>
                <strong>{$t('ui_developer_mode_b044b8b0')}</strong>
                <small> {$t('ui_show_technical_user_channel_and_message_ids_i_ed8ff2ba')} </small>
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
            <h2>{$t('ui_security_8f6fb4eb')}</h2>
            <p>{$t('ui_protect_access_to_your_account_and_recovery_d_8eecbc8e')}</p>
          </div>
        </div>

        <div class="settings-card security-card">
          <div class="settings-card-row">
            <div class="security-label">
              <span class="status-dot" class:enabled={profile?.mfa_enabled}></span>
              <div>
                <strong>{$t('ui_two_factor_authentication_b6824cc8')}</strong>
                <p>
                  {profile?.mfa_enabled
                    ? $t('ui_an_authenticator_is_required_when_you_sign_in_17635594')
                    : $t('ui_add_an_authenticator_app_and_one_time_recover_7781d8d4')}
                </p>
              </div>
            </div>
            <span class:positive-chip={profile?.mfa_enabled} class="status-chip">
              {profile?.mfa_enabled ? $t('ui_enabled_92c1cdfd') : $t('ui_not_enabled_2b0e8048')}
            </span>
          </div>

          {#if recoveryCodes.length}
            <div class="security-flow recovery-panel">
              <div>
                <strong>{$t('ui_save_these_recovery_codes_now_1f23ad39')}</strong>
                <p>{$t('ui_each_code_works_once_they_will_not_be_shown_a_8f995c76')}</p>
              </div>
              <div class="recovery-grid">
                {#each recoveryCodes as code (code)}<code>{code}</code>{/each}
              </div>
              <button
                class="secondary-button"
                type="button"
                onclick={() => copyValue(recoveryCodes.join('\n'), 'Recovery codes')}
              >
                <Icon name="copy" size={16} />{$t('ui_copy_all_codes_17220f23')}
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
                <strong>{$t('ui_connect_your_authenticator_9513a621')}</strong>
                <p>{$t('ui_enter_this_secret_manually_then_verify_the_si_628077c4')}</p>
              </div>
              <div class="secret-value">
                <code>{mfaSetup.secret}</code>
                <button
                  class="icon-button"
                  type="button"
                  aria-label={$t('ui_copy_authenticator_secret_21bc8873')}
                  onclick={() => copyValue(mfaSetup?.secret ?? '', 'Authenticator secret')}
                >
                  <Icon name="copy" size={17} />
                </button>
              </div>
              <label class="form-field compact-field">
                <span>{$t('ui_verification_code_3ee75029')}</span>
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
                  }}>{$t('ui_cancel_19766ed6')}</button
                >
                <button class="primary-button" disabled={busy}
                  >{$t('ui_enable_authenticator_0e248002')}</button
                >
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
                <strong>{$t('ui_replace_your_authenticator_4a8203ad')}</strong>
                <p>{$t('ui_confirm_your_password_and_current_factor_befo_42b719e7')}</p>
              </div>
              <div class="two-column-fields">
                <label class="form-field compact-field">
                  <span>{$t('ui_password_e7cf3ef4')}</span>
                  <input
                    bind:value={mfaPassword}
                    type="password"
                    autocomplete="current-password"
                    maxlength="256"
                    required
                  />
                </label>
                <label class="form-field compact-field">
                  <span>{$t('ui_current_authenticator_or_recovery_code_8221220e')}</span>
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
                  <Icon name="key" size={16} />{$t('ui_replace_authenticator_acf97cbe')}
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
                <strong>{$t('ui_disable_two_factor_authentication_c2d5063d')}</strong>
                <p>{$t('ui_this_requires_your_password_and_a_current_aut_e5c6b70a')}</p>
              </div>
              <div class="two-column-fields">
                <label class="form-field compact-field">
                  <span>{$t('ui_password_e7cf3ef4')}</span>
                  <input
                    bind:value={disablePassword}
                    type="password"
                    autocomplete="current-password"
                    maxlength="256"
                    required
                  />
                </label>
                <label class="form-field compact-field">
                  <span>{$t('ui_authenticator_code_eab11173')}</span>
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
                  >{$t('ui_disable_two_factor_authentication_c2d5063d')}</button
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
                <span>{$t('ui_confirm_your_password_bfd8c343')}</span>
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
                  <Icon name="key" size={16} />{$t('ui_set_up_authenticator_334d116a')}
                </button>
              </div>
            </form>
          {/if}
        </div>

        {#if profile}<E2EESettings user={profile} />{/if}

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
                <strong>{$t('ui_email_address_f2488fd4')}</strong>
                <p>
                  {profile?.email ?? $t('ui_no_email_address_f52f60b7')}
                  {#if profile?.email_verified}
                    <span class="verified-label"
                      ><Icon name="check" size={13} />{$t('ui_verified_4f783840')}</span
                    >
                  {/if}
                </p>
              </div>
              <Icon name="mail" />
            </div>
            <div class="two-column-fields">
              <label class="form-field compact-field">
                <span>{$t('ui_new_email_0d25c8b1')}</span>
                <input
                  bind:value={nextEmail}
                  type="email"
                  autocomplete="email"
                  maxlength="320"
                  required
                />
              </label>
              <label class="form-field compact-field">
                <span>{$t('ui_current_password_72ed2bd7')}</span>
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
              <button class="secondary-button" disabled={busy}
                >{$t('ui_send_confirmation_3233612b')}</button
              >
            </div>
          </form>
        {:else if emailEnabled === false}
          <div class="settings-card settings-card-row">
            <div>
              <strong>{$t('ui_email_free_account_1283de56')}</strong>
              <p>{$t('ui_this_instance_does_not_require_or_deliver_ema_e40096e9')}</p>
            </div>
            <Icon name="mail" />
          </div>
        {/if}
      </section>
    {/if}

    <footer class="settings-footer">
      <span>{$t('ui_kaede_chat_8f3c1776')}</span>
      <span
        >{$t('ui_your_handle_never_changes_value0_2d185933', {
          value0: String(profile?.handle ?? '—')
        })}</span
      >
    </footer>
  </section>
</main>
