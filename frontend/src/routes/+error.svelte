<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';

  const title = $derived(
    page.status === 404
      ? 'This thread went missing.'
      : page.status === 401
        ? 'Your session has ended.'
        : page.status === 403
          ? 'You cannot open this page.'
          : page.status === 429
            ? 'Too many requests.'
            : page.status >= 500
              ? 'Kaede lost the thread.'
              : 'We could not open this page.'
  );
  const description = $derived(
    page.status === 404
      ? 'The link may be old, private, or mistyped.'
      : page.status === 401
        ? 'Sign in again to continue. Nothing you entered on this page was saved.'
        : page.status === 403
          ? 'Your account does not have access. Ask a guild administrator if you think this is a mistake.'
          : page.status === 429
            ? 'Wait a moment, then try loading this page again.'
            : page.status >= 500
              ? 'The server could not complete this request. Try again; if it keeps happening, contact your administrator.'
              : 'Nothing you entered was lost. Return home or try loading this page again.'
  );
</script>

<svelte:head><title>{page.status} · Kaede Chat</title></svelte:head>

<main class="error-page">
  <section class="error-card">
    <span class="error-mark"><Icon name="message" size={28} /></span>
    <p class="eyebrow">Error {page.status}</p>
    <h1>{title}</h1>
    <p>{description}</p>
    <div class="welcome-actions">
      {#if page.status === 401}
        <a class="primary-button" href={resolve('/login')}>Sign in again</a>
      {:else}
        <a class="primary-button" href={resolve('/home')}>Return home</a>
      {/if}
      <button class="secondary-button" type="button" onclick={() => window.location.reload()}>
        Try again
      </button>
    </div>
  </section>
</main>
