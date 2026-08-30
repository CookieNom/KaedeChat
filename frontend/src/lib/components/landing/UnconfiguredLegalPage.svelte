<script lang="ts">
  import LegalPage from './LegalPage.svelte';

  let { title, policy }: { title: string; policy: string } = $props();
</script>

<LegalPage {title} updated={null}>
  <section>
    <h2>No operator policy is published</h2>
    <p>
      This build does not include an operator-authored {policy}. This notice is not a {policy} and does
      not create terms for an instance operator. Ask the operator of the instance you use for its current
      policies and contact details.
    </p>
  </section>
</LegalPage>
