<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';

  let { children } = $props();
  const onLogin = $derived(page.url.pathname.endsWith('/login'));
</script>

<main class="auth-shell">
  <header class="auth-topbar">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <a href={onLogin ? resolve('/register') : resolve('/login')}>
      {onLogin ? 'Create account' : 'Sign in'}
    </a>
  </header>
  <div class="auth-frame">
    <aside class="auth-story" aria-label="About Kaede Chat">
      <p class="eyebrow">Your home on the fediverse</p>
      <h2>One account.<br />Every community.</h2>
      <p>
        Choose the Kaede server that stores your account, then join communities hosted anywhere on
        the network.
      </p>
      <ul>
        <li><i></i>Independent and self-hosted</li>
        <li><i></i>Federated like email</li>
        <li><i></i>Built for real-time conversation</li>
      </ul>
    </aside>
    <section class="auth-paper">{@render children()}</section>
  </div>
  <footer class="auth-footer">
    <span>Kaede Chat</span>
    <span>Open communities, on your terms.</span>
  </footer>
</main>
