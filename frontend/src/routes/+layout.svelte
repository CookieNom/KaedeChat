<script lang="ts">
  import '../styles.css';
  import '@fontsource-variable/bricolage-grotesque';
  import '@fontsource-variable/inter';
  import '@fontsource-variable/jetbrains-mono';
  import { resolve } from '$app/paths';
  import { applyTheme, storedTheme } from '$lib/ui/theme';
  import { onMount } from 'svelte';

  let { children } = $props();

  onMount(() => {
    applyTheme(storedTheme(), false);
    const colorScheme = window.matchMedia('(prefers-color-scheme: dark)');
    const colorSchemeChanged = () => {
      if (storedTheme() === 'system') applyTheme('system', false);
    };
    const sessionExpired = () => window.location.replace(resolve('/login'));
    const pageRestored = (event: PageTransitionEvent) => {
      // Never reveal a protected in-memory view restored from the back-forward
      // cache after logout; reload it so the API session guard runs again.
      if (event.persisted) window.location.reload();
    };
    window.addEventListener('kaede:session-expired', sessionExpired);
    window.addEventListener('pageshow', pageRestored);
    colorScheme.addEventListener('change', colorSchemeChanged);
    return () => {
      window.removeEventListener('kaede:session-expired', sessionExpired);
      window.removeEventListener('pageshow', pageRestored);
      colorScheme.removeEventListener('change', colorSchemeChanged);
    };
  });
</script>

{@render children()}
