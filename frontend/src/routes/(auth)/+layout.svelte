<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { page } from '$app/state';
  import { resolve } from '$app/paths';

  let { children } = $props();
  const onLogin = $derived(page.url.pathname.endsWith('/login'));
  const onAccounts = $derived(page.url.pathname.endsWith('/accounts'));
</script>

<main class="auth-shell">
  <header class="auth-topbar">
    <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
    <a href={onLogin ? resolve('/register') : resolve('/login')}>
      {onLogin ? $t('ui_create_account_798ca2ce') : $t('ui_sign_in_bfd402b2')}
    </a>
  </header>
  <div class="auth-frame" class:account-frame={onAccounts}>
    {#if !onAccounts}
      <aside class="auth-story" aria-label={$t('ui_about_kaede_chat_8840db12')}>
        <p class="eyebrow">{$t('ui_your_home_on_the_fediverse_475b11bb')}</p>
        <h2>{$t('ui_one_account_3ca8c840')}<br />{$t('ui_every_community_8b59c50f')}</h2>
        <p>{$t('ui_choose_the_kaede_server_that_stores_your_acco_f994b9c0')}</p>
        <ul>
          <li><i></i>{$t('ui_independent_and_self_hosted_7e153df3')}</li>
          <li><i></i>{$t('ui_federated_like_email_c629c0da')}</li>
          <li><i></i>{$t('ui_built_for_real_time_conversation_0da33453')}</li>
        </ul>
      </aside>
    {/if}
    <section class="auth-paper">{@render children()}</section>
  </div>
  <footer class="auth-footer">
    <span>{$t('ui_kaede_chat_8f3c1776')}</span>
    <span>{$t('ui_open_communities_on_your_terms_1619deaa')}</span>
  </footer>
</main>

<style>
  .auth-frame.account-frame {
    width: min(560px, calc(100% - 24px));
    height: auto;
    max-height: none;
    grid-template-columns: minmax(0, 1fr);
    margin: 1.5rem auto;
    align-self: center;
  }

  .account-frame .auth-paper {
    padding: clamp(1.25rem, 4vw, 2.5rem);
  }
</style>
