<script lang="ts">
  import { resolve } from '$app/paths';
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
      title: 'Voice & video that just work',
      body: 'Hop into a voice room, start a video call, or share your screen. No dial-in codes, no waiting in a lobby.'
    },
    {
      icon: 'image' as const,
      title: 'Files, images & media',
      body: 'Send photos, files, and voice notes. Reactions, polls, threads, and stickers keep a channel feeling alive instead of like a ticket queue.'
    },
    {
      icon: 'globe' as const,
      title: 'Federated by design',
      body: 'Your handle works the way an email address does. Move between instances without rebuilding who you are.'
    },
    {
      icon: 'lock' as const,
      title: 'End-to-end encryption',
      body: 'For the rooms where it matters, messages are encrypted so that only the people in the room can read them.'
    },
    {
      icon: 'server' as const,
      title: 'Self-hosted',
      body: 'The whole platform runs on hardware we manage. The source is public, so you can check that it does what it says.'
    }
  ];

  const principles = [
    {
      title: 'We run the servers',
      body: 'This instance is operated by hand. The hardware, the moderation, and the policies are ours, and they are ours to answer for.'
    },
    {
      title: 'Your account is portable',
      body: 'Because Kaede federates, your handle and your identity travel with you if you ever decide to leave for another instance.'
    },
    {
      title: 'The rules are written down',
      body: 'Our terms of service and privacy policy are short, plain, and linked below. When something changes, we say so in the open.'
    },
    {
      title: 'You can always export',
      body: 'Your messages and media belong to you. We do not lock them in, and we do not sell them.'
    }
  ];
</script>

<svelte:head>
  <title>Kaede Chat — a home for real conversation</title>
  <meta
    name="description"
    content="Self-hosted, federated chat with voice, video, files, and end-to-end encryption. Run on servers we operate, open source, and free to join."
  />
</svelte:head>

<main class="landing-page cl-page">
  <header class="landing-nav cl-nav">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <nav aria-label="Page">
      <a href="#features">Features</a>
      <a href="#how-we-run">How we run it</a>
      <a href="#hosting">Hosting</a>
      <a class="cl-signin" href={resolve('/login')}>Sign in</a>
      <a class="secondary-button" href={resolve('/register')}>Create account</a>
    </nav>
  </header>

  <section class="cl-hero">
    <span class="landing-pill"><i></i>Self-hosted · Federated · Open source</span>
    <h1>Where <em>our</em> conversations live.</h1>
    <p>
      Kaede runs on servers we operate. It is a complete chat platform — text, voice, video, and
      files for communities that would rather answer to people than to an ad team.
    </p>
    <div class="welcome-actions cl-cta">
      <a class="primary-button large-button" href={resolve('/register')}>
        Create an account <Icon name="chevron-right" size={18} />
      </a>
      <a class="secondary-button large-button" href={resolve('/login')}>Sign in</a>
    </div>
    <p class="cl-assure">Free to join · No ad tracking · You can read the source</p>

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
      <p>Runs on servers we operate</p>
    </div>
    <div>
      <span><Icon name="globe" size={18} /></span>
      <p>Federated — join from any Kaede instance</p>
    </div>
    <div>
      <span><Icon name="headphones" size={18} /></span>
      <p>Text, voice, video &amp; screen share</p>
    </div>
  </section>

  <section class="cl-section" id="features">
    <div class="cl-section-head">
      <h2>Everything a community needs, nothing it doesn’t</h2>
      <p>
        Kaede is not a stripped-down chat with an afterthought bolted on. The things people expect
        from a good chat app are here and they work.
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
      <h2>How we run it</h2>
      <p>
        A few things we think you should know before you settle in. None of this is small print.
      </p>
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
        <h2>Running Kaede is a thing you can do</h2>
        <p>
          Kaede is open source and free to self-host. If you want a place like this for your own
          community, the source, the deployment guides, and a reference configuration are all
          public. You get to choose the operator, the policies, and the hardware — and your
          community can reach us through federation either way.
        </p>
      </div>
    </div>
  </section>

  <section class="cl-cta-band">
    <h2>Come say hello.</h2>
    <p>
      Create an account, find a community, and claim your corner. Already have a handle? Sign in and
      pick up where you left off.
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
      <p>A home for real conversation, run on servers we operate.</p>
    </div>
    <nav class="cl-footer-links" aria-label="Footer">
      <a href={resolve('/terms')}>Terms of service</a>
      <a href={resolve('/privacy')}>Privacy policy</a>
      <a href={resolve('/login')}>Sign in</a>
      <a href={resolve('/register')}>Create account</a>
    </nav>
    <p class="cl-footer-note">
      © {year} · Operated on our own servers. Kaede is open source software.
    </p>
  </footer>
</main>
