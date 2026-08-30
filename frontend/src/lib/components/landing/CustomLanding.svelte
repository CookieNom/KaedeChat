<script lang="ts">
  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import Icon from '$lib/components/Icon.svelte';

  const year = new Date().getFullYear();

  const features = [
    {
      icon: 'forum' as const,
      title: 'Communities & channels',
      body: 'Organise the spaces your groups need. Channels keep big rooms calm, threads keep the details close, and roles keep things under control.'
    },
    {
      icon: 'headphones' as const,
      title: 'Voice, video & screen sharing',
      body: 'When voice service is enabled, community rooms and calls can include audio, camera video, and screen sharing.'
    },
    {
      icon: 'image' as const,
      title: 'Files, images & media',
      body: 'Send photos, files, and voice notes. Reactions, polls, threads, and stickers keep a channel feeling alive instead of like a ticket queue.'
    },
    {
      icon: 'globe' as const,
      title: 'One home, many communities',
      body: 'Your account stays on the instance where you registered, while federation lets you join supported communities and conversations on other Kaede instances.'
    },
    {
      icon: 'lock' as const,
      title: 'Optional end-to-end encryption',
      body: 'Supported rooms can encrypt message content and media for their participants. Encryption does not hide account, delivery, membership, or other service metadata.'
    },
    {
      icon: 'server' as const,
      title: 'Independent instances',
      body: 'Each Kaede instance has its own operator, infrastructure, moderation, configuration, and policies. Features and data practices can differ between instances.'
    }
  ];

  const principles = [
    {
      title: 'Know your operator',
      body: operatorLegalConfig
        ? `${operatorLegalConfig.operatorName} operates ${operatorLegalConfig.instanceName}. Its infrastructure may include service providers described in the privacy policy.`
        : 'The operator of your home instance controls its infrastructure, moderation, and policies.'
    },
    {
      title: 'Your identity has a home',
      body: 'Your account is secured by this instance, and your domain-based handle identifies you across compatible Kaede communities.'
    },
    {
      title: 'Federation shares data',
      body: 'Using remote communities or contacting remote users sends the data needed for those interactions to other instances, whose operators have their own policies.'
    },
    {
      title: 'You have practical controls',
      body: 'You can update your profile and preferences, delete supported messages and media, manage notifications, and export an encrypted recovery bundle.'
    }
  ];
</script>

<svelte:head>
  <title>Kaede Chat — federated community chat</title>
  <meta
    name="description"
    content="Federated community chat with text, files, and optional voice, video, and end-to-end encryption."
  />
</svelte:head>

<main class="landing-page cl-page">
  <header class="landing-nav cl-nav">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <nav aria-label="Page">
      <a href="#features">Features</a>
      <a href="#how-we-run">What to know</a>
      <a href="#hosting">Hosting</a>
      <a class="cl-signin" href={resolve('/login')}>Sign in</a>
      <a class="secondary-button" href={resolve('/register')}>Create account</a>
    </nav>
  </header>

  <section class="cl-hero">
    <span class="landing-pill"><i></i>Federated community chat</span>
    <h1>A place for <em>our</em> conversations.</h1>
    <p>
      Kaede brings text channels, direct messages, files, and community tools together. Voice,
      video, screen sharing, search, and encrypted rooms are available when this instance enables
      them.
    </p>
    <div class="welcome-actions cl-cta">
      <a class="primary-button large-button" href={resolve('/register')}>
        Create an account <Icon name="chevron-right" size={18} />
      </a>
      <a class="secondary-button large-button" href={resolve('/login')}>Sign in</a>
    </div>
    <p class="cl-assure">Your account stays on this instance · Remote interactions federate</p>

    <div class="cl-preview" role="img" aria-label="A preview of a Kaede channel">
      <div class="cl-preview-bar">
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-dot" aria-hidden="true"></span>
        <span class="cl-preview-title"># general</span>
      </div>
      <div class="cl-preview-body">
        <div class="cl-msg">
          <span class="cl-avatar cl-avatar-pine">R</span>
          <div>
            <p class="cl-msg-head"><strong>riko</strong><small>14:02</small></p>
            <p class="cl-msg-text">Pushed the release notes. Give me ten and I’ll demo it.</p>
          </div>
        </div>
        <div class="cl-msg">
          <span class="cl-avatar cl-avatar-purple">M</span>
          <div>
            <p class="cl-msg-head"><strong>mio</strong><small>14:03</small></p>
            <p class="cl-msg-text">On my way. Dropping into the voice room now.</p>
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
      <p>Operated by {operatorLegalConfig?.operatorName ?? 'this instance’s operator'}</p>
    </div>
    <div>
      <span><Icon name="globe" size={18} /></span>
      <p>Federated with compatible Kaede instances</p>
    </div>
    <div>
      <span><Icon name="headphones" size={18} /></span>
      <p>Features depend on instance configuration</p>
    </div>
  </section>

  <section class="cl-section" id="features">
    <div class="cl-section-head">
      <h2>Built for community conversation</h2>
      <p>
        Kaede supports everyday community chat across web, desktop, and mobile. Some features
        require services that an instance operator may leave disabled.
      </p>
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
      <h2>What to know</h2>
      <p>The practical limits matter. Here is what the software supports today.</p>
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
        <h2>Kaede can run as an independent instance</h2>
        <p>
          The project includes deployment guides and a reference configuration for operating your
          own instance. An operator is responsible for its hosting, storage, moderation, security,
          legal obligations, and any optional third-party services it enables.
        </p>
      </div>
    </div>
  </section>

  <section class="cl-cta-band">
    <h2>Come say hello.</h2>
    <p>
      Create an account on this instance, or sign in with an account registered here. Accounts from
      another instance are not used to sign in here.
    </p>
    <div class="welcome-actions cl-cta">
      <a class="primary-button large-button" href={resolve('/register')}>
        Create an account <Icon name="chevron-right" size={18} />
      </a>
      <a class="secondary-button large-button" href={resolve('/login')}>Sign in</a>
    </div>
  </section>

  <footer class="cl-footer">
    <div class="cl-footer-brand">
      <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
      <p>
        Federated community chat operated by {operatorLegalConfig?.operatorName ??
          'this instance’s operator'}.
      </p>
    </div>
    <nav class="cl-footer-links" aria-label="Footer">
      <a href={resolve('/terms')}>Terms of service</a>
      <a href={resolve('/privacy')}>Privacy policy</a>
      <a href={resolve('/login')}>Sign in</a>
      <a href={resolve('/register')}>Create account</a>
    </nav>
    <p class="cl-footer-note">
      © {year} · Review the terms and privacy policy for this instance before registering.
    </p>
  </footer>
</main>
