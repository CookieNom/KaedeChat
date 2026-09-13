<script lang="ts">
  import { t } from '$lib/ui/locale';

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
      .catch(
        (caught) =>
          (error = userErrorMessage(caught, $t('ui_could_not_load_your_reports_37a1a5b1')))
      )
      .finally(() => (loaded = true));
  });
</script>

<svelte:head><title>{$t('ui_my_reports_kaede_chat_aee2e173')}</title></svelte:head>
<main>
  <header>
    <div>
      <span>{$t('ui_trust_safety_5b9c374d')}</span>
      <h1>{$t('ui_my_reports_cc6e3f45')}</h1>
    </div>
    <a href={resolve('/settings')}>{$t('ui_back_to_settings_dc9c6093')}</a>
  </header>
  <p class="intro">{$t('ui_reports_go_to_your_instance_s_trust_safety_te_d7a4025e')}</p>
  {#if error}<div class="notice" role="alert">{error}</div>{/if}
  {#if !loaded}<p>{$t('ui_loading_reports_4ef739f5')}</p>
  {:else if reports.length === 0}<section class="empty">
      <h2>{$t('ui_no_reports_c42bbbd2')}</h2>
      <p>{$t('ui_reports_you_submit_from_a_message_or_attachme_fe341f57')}</p>
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
          <small
            >{$t('ui_submitted_value0_58189b6e', {
              value0: String(new Date(report.created_at).toLocaleString())
            })}</small
          >
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
