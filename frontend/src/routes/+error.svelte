<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';

  const title = $derived(
    page.status === 404
      ? 'This thread went missing.'
      : page.status >= 500
        ? 'Kaede lost the thread.'
        : 'We could not open this page.'
  );
  const description = $derived(
    page.status === 404
      ? 'The link may be old, private, or mistyped.'
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
      <a class="primary-button" href={resolve('/home')}>Return home</a>
      <button class="secondary-button" type="button" onclick={() => window.location.reload()}>
        Try again
      </button>
    </div>
  </section>
</main>
