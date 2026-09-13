<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { onMount, tick } from 'svelte';

  let email = $state('');
  let sent = $state(false);
  let busy = $state(false);
  let error = $state('');
  let recoveryEnabled = $state<boolean | null>(null);
  let successPanel = $state<HTMLElement | null>(null);

  onMount(() => {
    const controller = new AbortController();
    void loadAuthConfiguration(controller.signal)
      .then((configuration) => {
        recoveryEnabled = configuration.password_recovery_enabled;
      })
      .catch(() => {
        recoveryEnabled = true;
      });
    return () => controller.abort();
  });

  async function submit() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await api('/auth/password/forgot', { method: 'POST', body: JSON.stringify({ email }) });
      sent = true;
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_request_a_reset_link_try_again_ecfc081b'));
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{$t('ui_reset_password_kaede_chat_2bf51d0f')}</title></svelte:head>
<p class="eyebrow">{$t('ui_account_recovery_2b291cd5')}</p>
<h1 class="auth-title">{$t('ui_find_your_way_back_0c4f428e')}</h1>
{#if recoveryEnabled === false}
  <p class="lede">{$t('ui_this_instance_does_not_use_email_so_self_serv_e9dbd6b1')}</p>
{:else if recoveryEnabled === null}
  <p class="lede">{$t('ui_checking_recovery_options_caf38bd6')}</p>
{:else if sent}<div
    bind:this={successPanel}
    class="auth-success"
    role="status"
    aria-live="polite"
    aria-atomic="true"
    tabindex="-1"
  >
    <p class="lede">{$t('ui_if_that_address_has_an_account_a_reset_link_i_480871ae')}</p>
    <p class="form-foot"><a href={resolve('/login')}>{$t('ui_return_to_sign_in_8504054f')}</a></p>
  </div>
{:else}<form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <label
      >{$t('ui_email_969ccbd3')}
      <input
        bind:value={email}
        type="email"
        autocomplete="email"
        maxlength="320"
        required
        disabled={busy}
      /></label
    >
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    <button class="primary-button" disabled={busy}
      >{busy ? $t('ui_sending_b8ed5279') : $t('ui_send_reset_link_708c5d67')}</button
    >
  </form>
  <p class="form-foot"><a href={resolve('/login')}>{$t('ui_back_to_sign_in_71fe31cc')}</a></p>{/if}
