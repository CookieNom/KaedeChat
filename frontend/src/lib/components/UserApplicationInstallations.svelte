<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { userErrorMessage } from '$lib/api/client';
  import {
    listUserApplicationInstallations,
    revokeUserApplicationInstallation,
    updateUserApplicationInstallation,
    userApplicationInstallationCanEditGrants,
    userApplicationInstallationUnavailableReason,
    type UserApplicationContext,
    type UserApplicationInstallation
  } from '$lib/chat/application-installations';
  import { assetUrl } from '$lib/media/assets';
  import { onMount } from 'svelte';

  let installations = $state<UserApplicationInstallation[]>([]);
  let loading = $state(true);
  let busyId = $state<string | null>(null);
  let error = $state('');
  let notice = $state('');

  onMount(() => {
    const controller = new AbortController();
    void listUserApplicationInstallations(controller.signal)
      .then((items) => (installations = items))
      .catch((caught) => {
        if (!controller.signal.aborted) {
          error = userErrorMessage(
            caught,
            $t('ui_could_not_load_your_authorized_apps_try_again_761f15e7')
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });
    return () => controller.abort();
  });

  function applicationDomain(installation: UserApplicationInstallation): string {
    return installation.application_ref.split('@').at(-1) ?? '';
  }

  async function toggleContext(
    installation: UserApplicationInstallation,
    context: UserApplicationContext,
    enabled: boolean
  ) {
    if (busyId || !userApplicationInstallationCanEditGrants(installation)) return;
    const contexts = enabled
      ? installation.contexts.includes(context)
        ? installation.contexts
        : [...installation.contexts, context]
      : installation.contexts.filter((item) => item !== context);
    if (!contexts.length) {
      error = $t('ui_keep_at_least_one_command_context_enabled_or__832cf59f');
      return;
    }
    busyId = installation.id;
    error = '';
    notice = '';
    try {
      const updated = await updateUserApplicationInstallation(installation.id, { contexts });
      installations = installations.map((item) => (item.id === updated.id ? updated : item));
      notice = `${installation.application_name} access updated.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_update_that_app_s_command_access_tr_06d900d7')
      );
    } finally {
      busyId = null;
    }
  }

  async function revoke(installation: UserApplicationInstallation) {
    if (
      busyId ||
      !window.confirm(
        `Revoke ${installation.application_name}? Its user-installed commands will disappear.`
      )
    ) {
      return;
    }
    busyId = installation.id;
    error = '';
    notice = '';
    try {
      await revokeUserApplicationInstallation(installation.id);
      installations = installations.filter((item) => item.id !== installation.id);
      notice = `${installation.application_name} was revoked.`;
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_revoke_that_app_try_again_1f2779e9'));
    } finally {
      busyId = null;
    }
  }
</script>

<section id="authorized-apps" class="authorized-apps">
  <header>
    <div>
      <h2>{$t('ui_authorized_apps_c3ecd1b4')}</h2>
      <p>{$t('ui_manage_apps_installed_for_your_account_and_wh_c1a417cb')}</p>
    </div>
  </header>

  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}

  <p class="install-guidance">
    {$t('ui_install_an_app_from_its_reviewed_78e20714')}
    <strong>{$t('ui_add_app_3de6a773')}</strong>
    {$t('ui_invitation_kaede_will_show_the_app_s_supporte_333b24e8')}
  </p>

  {#if loading}
    <p class="muted">{$t('ui_loading_authorized_apps_8e54fce1')}</p>
  {:else if installations.length === 0}
    <p class="muted">{$t('ui_you_have_not_installed_any_apps_for_your_acco_05df2fd5')}</p>
  {:else}
    <div class="installation-list">
      {#each installations as installation (installation.id)}
        {@const unavailableReason = userApplicationInstallationUnavailableReason(installation)}
        <article class:unavailable={!userApplicationInstallationCanEditGrants(installation)}>
          <div class="identity">
            <span class="icon">
              {#if installation.application_icon_hash}
                <img
                  src={assetUrl(
                    installation.application_icon_hash,
                    'thumbnail_128',
                    applicationDomain(installation)
                  )}
                  alt=""
                />
              {:else}
                {installation.application_name.slice(0, 1).toUpperCase()}
              {/if}
            </span>
            <span class="identity-copy">
              <span class="title-line">
                <strong>{installation.application_name}</strong>
                {#if installation.status !== 'active'}
                  <small class="status-chip">
                    {installation.status === 'suspended'
                      ? $t('ui_suspended_unavailable_e8a2540f')
                      : $t('ui_revoked_f6f738d0')}
                  </small>
                {/if}
              </span>
              <small>{installation.application_description ?? installation.application_ref}</small>
            </span>
          </div>
          {#if unavailableReason}
            <p class="unavailable-explanation" role="status">
              <strong>{$t('ui_commands_unavailable_bad5ded8')}</strong>
              <span>{unavailableReason}</span>
            </p>
          {/if}
          <fieldset
            disabled={busyId !== null || !userApplicationInstallationCanEditGrants(installation)}
          >
            <legend>{$t('ui_show_commands_in_0b6f8011')}</legend>
            <label>
              <input
                type="checkbox"
                checked={installation.contexts.includes('guild')}
                onchange={(event) =>
                  void toggleContext(installation, 'guild', event.currentTarget.checked)}
              />
              {$t('ui_guild_channels_a51396a0')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={installation.contexts.includes('private_channel')}
                onchange={(event) =>
                  void toggleContext(installation, 'private_channel', event.currentTarget.checked)}
              />
              {$t('ui_private_conversations_65f20ef7')}
            </label>
            <label>
              <input
                type="checkbox"
                checked={installation.contexts.includes('bot_dm')}
                onchange={(event) =>
                  void toggleContext(installation, 'bot_dm', event.currentTarget.checked)}
              />
              {$t('ui_direct_messages_with_bots_90dacb89')}
            </label>
          </fieldset>
          <footer>
            <small>{installation.scopes.join(' · ')}</small>
            <button
              class="revoke"
              type="button"
              disabled={busyId !== null}
              onclick={() => void revoke(installation)}
            >
              {busyId === installation.id ? $t('ui_saving_23e39291') : $t('ui_revoke_87e6d00b')}
            </button>
          </footer>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .authorized-apps {
    display: grid;
    gap: 14px;
  }
  header h2,
  header p,
  .identity strong,
  .identity small {
    margin: 0;
  }
  header p,
  .identity small,
  .muted,
  footer small,
  .install-guidance {
    color: var(--text-muted);
  }
  article {
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px;
    background: var(--surface);
  }
  article.unavailable {
    border-color: color-mix(in srgb, var(--warning) 38%, var(--line));
  }
  .install-guidance {
    margin: 0;
    font-size: 0.82rem;
    line-height: 1.45;
  }
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 12px;
    color: var(--text);
    background: var(--surface-raised);
    font: inherit;
    font-weight: 750;
    cursor: pointer;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .installation-list {
    display: grid;
    gap: 10px;
  }
  article {
    display: grid;
    gap: 12px;
  }
  .identity {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 10px;
  }
  .identity > span:last-child {
    display: grid;
    min-width: 0;
    gap: 2px;
  }
  .title-line {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 8px;
  }
  .title-line strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .status-chip {
    flex: 0 0 auto;
    border-radius: 999px;
    padding: 4px 7px;
    color: var(--warning);
    background: var(--warning-soft);
    font-size: 0.68rem;
    font-weight: 800;
    line-height: 1;
    text-transform: uppercase;
  }
  .unavailable-explanation {
    display: grid;
    gap: 3px;
    margin: 0;
    border-radius: 9px;
    padding: 10px 12px;
    color: var(--warning);
    background: var(--warning-soft);
    font-size: 0.78rem;
    line-height: 1.4;
  }
  .unavailable-explanation strong {
    font-size: 0.8rem;
  }
  .icon {
    display: grid;
    width: 42px;
    height: 42px;
    flex: 0 0 auto;
    place-items: center;
    overflow: hidden;
    border-radius: 10px;
    color: white;
    background: var(--accent);
    font-weight: 850;
  }
  .icon img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  fieldset {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    border: 0;
    padding: 0;
  }
  legend {
    width: 100%;
    margin-bottom: 4px;
    color: var(--text-muted);
    font-size: 0.72rem;
    font-weight: 750;
  }
  fieldset label {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 0.82rem;
  }
  fieldset input {
    width: 17px;
    height: 17px;
    accent-color: var(--accent);
  }
  fieldset:disabled label {
    color: var(--text-muted);
  }
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .revoke,
  .error {
    color: var(--danger);
  }
  .notice {
    color: var(--success);
  }
  .error,
  .notice,
  .muted {
    margin: 0;
    font-size: 0.82rem;
  }
  @media (max-width: 520px) {
    footer {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
