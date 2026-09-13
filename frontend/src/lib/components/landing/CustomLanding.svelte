<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import Icon from '$lib/components/Icon.svelte';

  const year = new Date().getFullYear();

  let features = $derived([
    {
      icon: 'forum' as const,
      title: $t('ui_communities_channels_76e49218'),
      body: 'Organise the spaces your groups need. Channels keep big rooms calm, threads keep the details close, and roles keep things under control.'
    },
    {
      icon: 'headphones' as const,
      title: $t('ui_voice_video_screen_sharing_a8a8605e'),
      body: 'When voice service is enabled, community rooms and calls can include audio, camera video, and screen sharing.'
    },
    {
      icon: 'image' as const,
      title: $t('ui_files_images_media_caaa4b43'),
      body: 'Send photos, files, and voice notes. Reactions, polls, threads, and stickers keep a channel feeling alive instead of like a ticket queue.'
    },
    {
      icon: 'globe' as const,
      title: $t('ui_one_home_many_communities_1885edc5'),
      body: 'Your account stays on the instance where you registered, while federation lets you join supported communities and conversations on other Kaede instances.'
    },
    {
      icon: 'lock' as const,
      title: $t('ui_optional_end_to_end_encryption_fe7a72de'),
      body: 'Supported rooms can encrypt message content and media for their participants. Encryption does not hide account, delivery, membership, or other service metadata.'
    },
    {
      icon: 'server' as const,
      title: $t('ui_independent_instances_0272a34c'),
      body: 'Each Kaede instance has its own operator, infrastructure, moderation, configuration, and policies. Features and data practices can differ between instances.'
    }
  ]);

  let principles = $derived([
    {
      title: $t('ui_know_your_operator_1c7f0ab9'),
      body: operatorLegalConfig
        ? `${operatorLegalConfig.operatorName} operates ${operatorLegalConfig.instanceName}. Its infrastructure may include service providers described in the privacy policy.`
        : 'The operator of your home instance controls its infrastructure, moderation, and policies.'
    },
    {
      title: $t('ui_your_identity_has_a_home_088a6b55'),
      body: 'Your account is secured by this instance, and your domain-based handle identifies you across compatible Kaede communities.'
    },
    {
      title: $t('ui_federation_shares_data_22f37149'),
      body: 'Using remote communities or contacting remote users sends the data needed for those interactions to other instances, whose operators have their own policies.'
    },
    {
      title: $t('ui_you_have_practical_controls_824a3405'),
      body: 'You can update your profile and preferences, delete supported messages and media, manage notifications, and export an encrypted recovery bundle.'
    }
  ]);
</script>

<svelte:head>
  <title>{$t('ui_kaede_chat_federated_community_chat_8a9c2162')}</title>
  <meta
    name="description"
    content="Federated community chat with text, files, and optional voice, video, and end-to-end encryption."
  />
</svelte:head>

<main class="landing-page cl-page">
  <header class="landing-nav cl-nav">
    <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
    <nav aria-label={$t('ui_page_0a30a815')}>
      <a href="#features">{$t('ui_features_5697d03d')}</a>
      <a href="#how-we-run">{$t('ui_what_to_know_8248a6c6')}</a>
      <a href="#hosting">{$t('ui_hosting_b4ec9bba')}</a>
      <a class="cl-signin" href={resolve('/login')}>{$t('ui_sign_in_bfd402b2')}</a>
      <a class="secondary-button" href={resolve('/register')}>{$t('ui_create_account_798ca2ce')}</a>
    </nav>
  </header>

  <section class="cl-hero">
    <span class="landing-pill"><i></i>{$t('ui_federated_community_chat_194534b4')}</span>
    <h1>{$t('ui_a_place_for_a8c902c5')} <em>our</em> conversations.</h1>
    <p>{$t('ui_kaede_brings_text_channels_direct_messages_fi_d2c15c22')}</p>
    <div class="welcome-actions cl-cta">
      <a class="primary-button large-button" href={resolve('/register')}>
        {$t('ui_create_an_account_86033f75')}
        <Icon name="chevron-right" size={18} />
      </a>
      <a class="secondary-button large-button" href={resolve('/login')}
        >{$t('ui_sign_in_bfd402b2')}</a
      >
    </div>
    <p class="cl-assure">{$t('ui_your_account_stays_on_this_instance_remote_in_7a26c2c0')}</p>

    <div class="cl-preview" role="img" aria-label={$t('ui_a_preview_of_a_kaede_channel_3198a834')}>
      <div class="cl-preview-bar">
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-title">{$t('ui_general_ad7dd301')}</span>
      </div>
      <div class="cl-preview-body">
        <div class="cl-msg">
          <span class="cl-avatar cl-avatar-pine">R</span>
          <div>
            <p class="cl-msg-head"><strong>riko</strong><small>14:02</small></p>
            <p class="cl-msg-text">
              {$t('ui_pushed_the_release_notes_give_me_ten_and_i_ll_d048f2a4')}
            </p>
          </div>
        </div>
        <div class="cl-msg">
          <span class="cl-avatar cl-avatar-purple">M</span>
          <div>
            <p class="cl-msg-head"><strong>mio</strong><small>14:03</small></p>
            <p class="cl-msg-text">
              {$t('ui_on_my_way_dropping_into_the_voice_room_now_0e450632')}
            </p>
          </div>
        </div>
        <div class="cl-msg cl-msg-typing">
          <span class="cl-avatar cl-avatar-accent">A</span>
          <div>
            <p class="cl-msg-head"><strong>akari</strong></p>
            <p class="cl-msg-text cl-typing"><i></i><i></i><i></i></p>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="cl-trust">
    <div>
      <span><Icon name="server" size={18} /></span>
      <p>
        {$t('ui_operated_by_value0_1a139472', {
          value0: String(operatorLegalConfig?.operatorName ?? 'this instance’s operator')
        })}
      </p>
    </div>
    <div>
      <span><Icon name="globe" size={18} /></span>
      <p>{$t('ui_federated_with_compatible_kaede_instances_2b173c45')}</p>
    </div>
    <div>
      <span><Icon name="headphones" size={18} /></span>
      <p>{$t('ui_features_depend_on_instance_configuration_c7594cfa')}</p>
    </div>
  </section>

  <section class="cl-section" id="features">
    <div class="cl-section-head">
      <h2>{$t('ui_built_for_community_conversation_e877e566')}</h2>
      <p>{$t('ui_kaede_supports_everyday_community_chat_across_67650733')}</p>
    </div>
    <div class="cl-grid">
      {#each features as feature (feature.title)}
        <article>
          <span><Icon name={feature.icon} /></span>
          <h3>{feature.title}</h3>
          <p>{feature.body}</p>
        </article>
      {/each}
    </div>
  </section>

  <section class="cl-section cl-section-alt" id="how-we-run">
    <div class="cl-section-head">
      <h2>{$t('ui_what_to_know_8248a6c6')}</h2>
      <p>{$t('ui_the_practical_limits_matter_here_is_what_the__a3d35bf2')}</p>
    </div>
    <ol class="cl-principles">
      {#each principles as principle, index (principle.title)}
        <li>
          <span class="cl-principle-num">{index + 1}</span>
          <div>
            <h3>{principle.title}</h3>
            <p>{principle.body}</p>
          </div>
        </li>
      {/each}
    </ol>
  </section>

  <section class="cl-section" id="hosting">
    <div class="cl-host">
      <span class="cl-host-icon"><Icon name="shield" /></span>
      <div class="cl-host-copy">
        <h2>{$t('ui_kaede_can_run_as_an_independent_instance_608ff5ad')}</h2>
        <p>{$t('ui_the_project_includes_deployment_guides_and_a__306fee86')}</p>
      </div>
    </div>
  </section>

  <section class="cl-cta-band">
    <h2>{$t('ui_come_say_hello_c43cf6b7')}</h2>
    <p>{$t('ui_create_an_account_on_this_instance_or_sign_in_abde1861')}</p>
    <div class="welcome-actions cl-cta">
      <a class="primary-button large-button" href={resolve('/register')}>
        {$t('ui_create_an_account_86033f75')}
        <Icon name="chevron-right" size={18} />
      </a>
      <a class="secondary-button large-button" href={resolve('/login')}
        >{$t('ui_sign_in_bfd402b2')}</a
      >
    </div>
  </section>

  <footer class="cl-footer">
    <div class="cl-footer-brand">
      <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
      <p>
        {$t('ui_federated_community_chat_operated_by_value0_565b3b7e', {
          value0: String(operatorLegalConfig?.operatorName ?? 'this instance’s operator')
        })}
      </p>
    </div>
    <nav class="cl-footer-links" aria-label={$t('ui_footer_26c87bb5')}>
      <a href={resolve('/terms')}>{$t('ui_terms_of_service_e69e0614')}</a>
      <a href={resolve('/privacy')}>{$t('ui_privacy_policy_ba445cff')}</a>
      <a href={resolve('/login')}>{$t('ui_sign_in_bfd402b2')}</a>
      <a href={resolve('/register')}>{$t('ui_create_account_798ca2ce')}</a>
    </nav>
    <p class="cl-footer-note">
      {$t('ui_value0_review_the_terms_and_privacy_policy_fo_cdc785d5', { value0: String(year) })}
    </p>
  </footer>
</main>
