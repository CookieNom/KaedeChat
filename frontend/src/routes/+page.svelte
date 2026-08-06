<script lang="ts">
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';
</script>

<svelte:head>
  <title>Kaede Chat</title>
  <meta name="description" content="Your conversations, on your terms." />
</svelte:head>

<main class="landing-page">
  <header class="landing-nav">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <nav aria-label="Account">
      <a href={resolve('/login')}>Sign in</a>
      <a class="secondary-button" href={resolve('/register')}>Create account</a>
    </nav>
  </header>

  <section class="landing-hero">
    <div class="landing-copy">
      <span class="landing-pill"><i></i>Independent social chat</span>
      <h1>Conversation that belongs to <em>you.</em></h1>
      <p>
        Kaede brings communities, messages, voice, and video together without putting every person
        on one company’s server.
      </p>
      <div class="welcome-actions">
        <a class="primary-button large-button" href={resolve('/register')}>
          Find your place <Icon name="chevron-right" size={18} />
        </a>
        <a class="secondary-button large-button" href={resolve('/login')}>I already belong here</a>
      </div>
      <dl class="landing-stats">
        <div>
          <dt>Your identity</dt>
          <dd>Stays with your home instance</dd>
        </div>
        <div>
          <dt>Your communities</dt>
          <dd>Can live anywhere on the network</dd>
        </div>
      </dl>
    </div>

    <div
      class="federation-visual"
      role="img"
      aria-label="A federated conversation across three instances"
    >
      <div class="federation-glow" aria-hidden="true"></div>
      <article class="instance-card instance-primary">
        <header>
          <span>K</span>
          <div><strong>Your instance</strong><small>home.example</small></div>
        </header>
        <div class="mini-channel active"><i>#</i><span>general</span></div>
        <div class="mini-channel"><i>#</i><span>projects</span></div>
        <div class="mini-message">
          <span>A</span>
          <p><strong>akari</strong><small>Ready when you are ✦</small></p>
        </div>
      </article>
      <article class="instance-card instance-remote-one">
        <header>
          <span>N</span>
          <div><strong>Night Garden</strong><small>garden.social</small></div>
        </header>
        <p><Icon name="globe" size={15} />Federated guild</p>
      </article>
      <article class="instance-card instance-remote-two">
        <header>
          <span>F</span>
          <div><strong>Friends</strong><small>elsewhere.net</small></div>
        </header>
        <p><Icon name="message" size={15} />Direct messages</p>
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
      <h2>Host your own home</h2>
      <p>Choose your operator, policies, storage, and domain without isolating your community.</p>
    </article>
    <article>
      <span><Icon name="globe" /></span>
      <h2>Federate naturally</h2>
      <p>Handles work like email addresses. Guilds and DMs cross instance boundaries securely.</p>
    </article>
    <article>
      <span><Icon name="message" /></span>
      <h2>Talk in real time</h2>
      <p>Rich messages, files, voice, video, and screen sharing live in one focused interface.</p>
    </article>
  </section>

  <footer class="landing-footer">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <p>Built for communities that want a place of their own.</p>
  </footer>
</main>
