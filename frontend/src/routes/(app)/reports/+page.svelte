<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';

  interface Report {
    id: string;
    target_type: string;
    target_ref: string;
    category: string;
    description: string | null;
    status: string;
    created_at: string;
    updated_at: string;
  }

  let reports = $state<Report[]>([]);
  let loaded = $state(false);
  let error = $state('');

  onMount(() => {
    void api<Report[]>('/reports/@me')
      .then((value) => (reports = value))
      .catch((caught) => (error = userErrorMessage(caught, 'Could not load your reports.')))
      .finally(() => (loaded = true));
  });
</script>

<svelte:head><title>My reports · Kaede Chat</title></svelte:head>
<main>
  <header>
    <div>
      <span>Trust &amp; Safety</span>
      <h1>My reports</h1>
    </div>
    <a href={resolve('/settings')}>Back to settings</a>
  </header>
  <p class="intro">
    Reports go to your instance's Trust &amp; Safety team. Guild moderators do not receive them.
    Encrypted message text cannot be submitted because your instance cannot read it.
  </p>
  {#if error}<div class="notice" role="alert">{error}</div>{/if}
  {#if !loaded}<p>Loading reports…</p>
  {:else if reports.length === 0}<section class="empty">
      <h2>No reports</h2>
      <p>Reports you submit from a message's menu will appear here.</p>
    </section>
  {:else}<div class="reports">
      {#each reports as report (report.id)}<article>
          <header>
            <strong>{report.category.replaceAll('_', ' ')}</strong><span
              >{report.status.replaceAll('_', ' ')}</span
            >
          </header>
          <p>{report.target_type} · {report.target_ref}</p>
          {#if report.description}<blockquote>{report.description}</blockquote>{/if}
          <small>Submitted {new Date(report.created_at).toLocaleString()}</small>
        </article>{/each}
    </div>{/if}
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  main {
    box-sizing: border-box;
    width: min(850px, 100%);
    min-height: 100dvh;
    margin: auto;
    padding: clamp(1.2rem, 5vw, 4rem);
    color: var(--text);
  }
  main > header,
  article header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  h1 {
    margin: 0.2rem 0;
  }
  main > header span {
    color: var(--accent);
    font-size: 0.75rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  a {
    color: var(--accent);
  }
  .intro,
  article p,
  article small,
  .empty p {
    color: var(--text-muted);
  }
  .reports {
    display: grid;
    gap: 0.8rem;
    margin-top: 2rem;
  }
  article,
  .empty,
  .notice {
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
    background: var(--surface);
  }
  article header span {
    border-radius: 999px;
    padding: 0.2rem 0.55rem;
    background: var(--surface-hover);
    text-transform: capitalize;
  }
  article p {
    margin: 0.5rem 0;
  }
  blockquote {
    margin: 0.8rem 0;
    padding-left: 0.8rem;
    border-left: 3px solid var(--line);
  }
  .notice {
    color: var(--danger, #ef6767);
  }
</style>
