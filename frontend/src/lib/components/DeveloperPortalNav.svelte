<script lang="ts">
  import { resolve } from '$app/paths';

  let { active }: { active: 'applications' | 'teams' } = $props();
</script>

<aside class="developer-nav">
  <a class="back" href={resolve('/settings')}>← Back to Kaede</a>
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">K</span>
    <div>
      <small>Kaede</small>
      <strong>Developer Portal</strong>
    </div>
  </div>
  <nav aria-label="Developer Portal">
    <a class:active={active === 'applications'} href={resolve('/developers')}>
      <span aria-hidden="true">▦</span>
      <span><strong>Applications</strong><small>Build and configure bots</small></span>
    </a>
    <a class:active={active === 'teams'} href={resolve('/developers/teams')}>
      <span aria-hidden="true">♟</span>
      <span><strong>Teams</strong><small>People and access</small></span>
    </a>
  </nav>
  <div class="resources">
    <small>Resources</small>
    <a href={resolve('/docs/bots-and-automations')}>Bot API guide ↗</a>
  </div>
</aside>

<style>
  .developer-nav {
    box-sizing: border-box;
    position: sticky;
    top: 0;
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100dvh;
    padding: 1.25rem 1rem;
    border-right: 1px solid var(--line);
    background: var(--surface);
  }
  .back {
    align-self: flex-start;
    margin-bottom: 1.5rem;
    color: var(--text-muted);
    font-size: 0.82rem;
    text-decoration: none;
  }
  .back:hover,
  .resources a:hover {
    color: var(--accent);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 1.75rem;
    padding: 0 0.45rem;
  }
  .brand-mark {
    display: grid;
    width: 40px;
    height: 40px;
    place-items: center;
    border-radius: 12px;
    color: var(--on-accent, #fff);
    background: var(--accent);
    font-weight: 900;
  }
  .brand div,
  nav a > span:last-child {
    display: grid;
  }
  .brand small,
  nav small,
  .resources > small {
    color: var(--text-muted);
    font-size: 0.72rem;
  }
  nav {
    display: grid;
    gap: 0.35rem;
  }
  nav a {
    display: grid;
    grid-template-columns: 28px 1fr;
    gap: 0.55rem;
    align-items: center;
    border-radius: 10px;
    padding: 0.7rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  nav a:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  nav a.active {
    color: var(--text);
    background: var(--accent-soft);
  }
  nav a > span:first-child {
    text-align: center;
    font-size: 1.05rem;
  }
  nav strong {
    font-size: 0.9rem;
  }
  .resources {
    display: grid;
    gap: 0.5rem;
    margin-top: auto;
    padding: 1rem 0.7rem 0;
    border-top: 1px solid var(--line);
  }
  .resources a {
    color: var(--text-muted);
    font-size: 0.82rem;
    text-decoration: none;
  }
  @media (max-width: 820px) {
    .developer-nav {
      position: static;
      height: auto;
      padding: 0.8rem 1rem;
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
    .back,
    .brand,
    .resources {
      display: none;
    }
    nav {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    nav a {
      padding: 0.55rem 0.7rem;
    }
    nav small {
      display: none;
    }
  }
</style>
