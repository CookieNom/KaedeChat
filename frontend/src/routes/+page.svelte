<script lang="ts">
  import { isCustomLanding } from '$lib/branding/landing';
  import CustomLanding from '$lib/components/landing/CustomLanding.svelte';
  import DefaultLanding from '$lib/components/landing/DefaultLanding.svelte';
</script>

{#if isCustomLanding}
  <CustomLanding />
{:else}
  <DefaultLanding />
{/if}
