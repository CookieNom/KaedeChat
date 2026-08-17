<script lang="ts">
  import '../styles.css';
  import '@fontsource-variable/bricolage-grotesque';
  import '@fontsource-variable/inter';
  import '@fontsource-variable/jetbrains-mono';
  import { afterNavigate, goto } from '$app/navigation';
  import { resolve } from '$app/paths';
  import {
    initializeNativeInstance,
    isNativeDesktop,
    rememberNativeRoute,
    storedNativeRoute
  } from '$lib/platform/native';
  import { applyTheme, storedTheme } from '$lib/ui/theme';
  import NativeDesktopLifecycle from '$lib/components/NativeDesktopLifecycle.svelte';
  import { onMount } from 'svelte';

  let { children } = $props();

  afterNavigate(({ to }) => {
    if (to) rememberNativeRoute(`${to.url.pathname}${to.url.search}${to.url.hash}`);
  });

  onMount(() => {
    // Begin restoring a native session before any protected child route can
    // issue work. API calls also await the same single-flight promise.
    void initializeNativeInstance()
      .then((restored) => {
        if (!restored.authenticated || !isNativeDesktop()) return;
        const current = window.location.pathname;
        if (
          current === resolve('/') ||
          current === resolve('/login') ||
          current === resolve('/register') ||
          current === resolve('/forgot-password')
        ) {
          // eslint-disable-next-line svelte/no-navigation-without-resolve -- storedNativeRoute accepts only validated in-app paths and the fallback is resolved.
          void goto(storedNativeRoute() ?? resolve('/home'), { replaceState: true });
        }
      })
      .catch(() => {
        // The request layer retries transient native-vault failures and exposes
        // a useful error if the store remains unavailable.
      });
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
<NativeDesktopLifecycle />
