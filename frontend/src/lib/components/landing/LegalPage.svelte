<script lang="ts">
  import type { Snippet } from 'svelte';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';

  let { title, updated, children }: { title: string; updated: string | null; children: Snippet } =
    $props();
</script>

<svelte:head>
  <title>{title} · Kaede Chat</title>
</svelte:head>

<div class="legal-page">
  <header class="legal-topbar">
    <a class="wordmark" href={resolve('/')}><span>K</span>Kaede Chat</a>
    <a class="legal-back" href={resolve('/')}>
      <Icon name="arrow-left" size={16} /> Home
    </a>
  </header>

  <article class="legal-body">
    <h1>{title}</h1>
    {#if updated}
      <p class="legal-updated">Last updated {updated}</p>
    {/if}
    {@render children()}
  </article>

  <footer class="legal-footer">
    <nav class="legal-footer-links" aria-label="Legal">
      <a href={resolve('/terms')}>Terms of service</a>
      <a href={resolve('/privacy')}>Privacy policy</a>
    </nav>
    <a class="legal-back" href={resolve('/')}>
      <Icon name="arrow-left" size={16} /> Back to home
    </a>
  </footer>
</div>
