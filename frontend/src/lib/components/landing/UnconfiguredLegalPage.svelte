<script lang="ts">
  import { t } from '$lib/ui/locale';

  import LegalPage from './LegalPage.svelte';

  let { title, policy }: { title: string; policy: string } = $props();
</script>

<LegalPage {title} updated={null}>
  <section>
    <h2>{$t('ui_no_operator_policy_is_published_2a9a7f69')}</h2>
    <p>
      {$t('ui_this_build_does_not_include_an_operator_revie_de2c8ec8', {
        value0: String(policy),
        value1: String(policy)
      })}
    </p>
  </section>
</LegalPage>
