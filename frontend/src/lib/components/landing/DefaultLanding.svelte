<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';
</script>

<svelte:head>
  <title>{$t('ui_kaede_chat_8f3c1776')}</title>
  <meta name="description" content="Your conversations, on your terms." />
</svelte:head>

<main class="landing-page">
  <header class="landing-nav">
    <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
    <nav aria-label={$t('ui_account_7e1b0d56')}>
      <a href={resolve('/login')}>{$t('ui_sign_in_bfd402b2')}</a>
      <a class="secondary-button" href={resolve('/register')}>{$t('ui_create_account_798ca2ce')}</a>
    </nav>
  </header>

  <section class="landing-hero">
    <div class="landing-copy">
      <span class="landing-pill"><i></i>{$t('ui_independent_social_chat_946c3a8e')}</span>
      <h1>{$t('ui_conversation_that_belongs_to_baa04b61')} <em>you.</em></h1>
      <p>{$t('ui_kaede_brings_communities_messages_voice_and_v_e00a751e')}</p>
      <div class="welcome-actions">
        <a class="primary-button large-button" href={resolve('/register')}>
          {$t('ui_find_your_place_048d6aa2')}
          <Icon name="chevron-right" size={18} />
        </a>
        <a class="secondary-button large-button" href={resolve('/login')}
          >{$t('ui_i_already_belong_here_77322fc3')}</a
        >
      </div>
      <dl class="landing-stats">
        <div>
          <dt>{$t('ui_your_identity_3b421546')}</dt>
          <dd>{$t('ui_stays_on_the_server_where_you_signed_up_e1676fd3')}</dd>
        </div>
        <div>
          <dt>{$t('ui_your_communities_605352da')}</dt>
          <dd>{$t('ui_can_live_anywhere_on_the_network_3f624cc5')}</dd>
        </div>
      </dl>
    </div>

    <div
      class="federation-visual"
      role="img"
      aria-label={$t('ui_a_federated_conversation_across_three_instanc_a331cd45')}
    >
      <div class="federation-glow" aria-hidden="true"></div>
      <article class="instance-card instance-primary">
        <header>
          <span>K</span>
          <div><strong>{$t('ui_your_instance_2400b87f')}</strong><small>home.example</small></div>
        </header>
        <div class="mini-channel active"><i>#</i><span>general</span></div>
        <div class="mini-channel"><i>#</i><span>projects</span></div>
        <div class="mini-message">
          <span>A</span>
          <p><strong>akari</strong><small>{$t('ui_ready_when_you_are_5e3cd9fb')}</small></p>
        </div>
      </article>
      <article class="instance-card instance-remote-one">
        <header>
          <span>N</span>
          <div><strong>{$t('ui_night_garden_cc96dc8d')}</strong><small>garden.social</small></div>
        </header>
        <p><Icon name="globe" size={15} />{$t('ui_federated_guild_3b5de1bd')}</p>
      </article>
      <article class="instance-card instance-remote-two">
        <header>
          <span>F</span>
          <div><strong>{$t('ui_friends_bd104d1b')}</strong><small>elsewhere.net</small></div>
        </header>
        <p><Icon name="message" size={15} />{$t('ui_direct_messages_95e66705')}</p>
      </article>
      <svg class="federation-lines" viewBox="0 0 560 500" aria-hidden="true">
        <path d="M188 184C272 126 342 126 412 164" />
        <path d="M192 282C285 318 350 335 424 328" />
      </svg>
    </div>
  </section>

  <section class="landing-features">
    <article>
      <span><Icon name="server" /></span>
      <h2>{$t('ui_host_your_own_home_583f8bff')}</h2>
      <p>{$t('ui_choose_your_operator_policies_storage_and_dom_2a463238')}</p>
    </article>
    <article>
      <span><Icon name="globe" /></span>
      <h2>{$t('ui_federate_naturally_b43b2795')}</h2>
      <p>{$t('ui_handles_work_like_email_addresses_guilds_and__60b4dc79')}</p>
    </article>
    <article>
      <span><Icon name="message" /></span>
      <h2>{$t('ui_talk_in_real_time_f86a0b80')}</h2>
      <p>{$t('ui_rich_messages_files_voice_video_and_screen_sh_d02d2b27')}</p>
    </article>
  </section>

  <footer class="landing-footer">
    <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
    <p>{$t('ui_built_for_communities_that_want_a_place_of_th_f7a42867')}</p>
  </footer>
</main>
