<script lang="ts">
  import { t } from '$lib/ui/locale';

  import type { Snippet } from 'svelte';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';

  let { title, updated, children }: { title: string; updated: string | null; children: Snippet } =
    $props();
</script>

<svelte:head>
  <title>{$t('ui_value0_kaede_chat_4bf52868', { value0: String(title) })}</title>
</svelte:head>

<div class="legal-page">
  <header class="legal-topbar">
    <a class="wordmark" href={resolve('/')}><span>K</span>{$t('ui_kaede_chat_8f3c1776')}</a>
    <a class="legal-back" href={resolve('/')}>
      <Icon name="arrow-left" size={16} />
      {$t('ui_home_3a786953')}
    </a>
  </header>

  <article class="legal-body">
    <h1>{title}</h1>
    {#if updated}
      <p class="legal-updated">
        {$t('ui_last_updated_value0_f4459dce', { value0: String(updated) })}
      </p>
    {/if}
    {@render children()}
  </article>

  <footer class="legal-footer">
    <nav class="legal-footer-links" aria-label={$t('ui_legal_4787eaf7')}>
      <a href={resolve('/terms')}>{$t('ui_terms_of_service_e69e0614')}</a>
      <a href={resolve('/privacy')}>{$t('ui_privacy_policy_ba445cff')}</a>
    </nav>
    <a class="legal-back" href={resolve('/')}>
      <Icon name="arrow-left" size={16} />
      {$t('ui_back_to_home_76c5505f')}
    </a>
  </footer>
</div>
