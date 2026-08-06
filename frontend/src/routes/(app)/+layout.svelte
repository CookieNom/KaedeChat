<script lang="ts">
  import { api } from '$lib/api/client';
  import CommandSwitcher from '$lib/components/CommandSwitcher.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { applyLocale } from '$lib/ui/locale';
  import { applyTheme, type ThemePreference } from '$lib/ui/theme';
  import { onMount } from 'svelte';

  let { children } = $props();

  onMount(() => {
    const controller = new AbortController();
    developerMode.reset();
    authenticatedGateway.start();
    void api<{
      theme: ThemePreference;
      locale: string;
      notification_settings: Record<string, unknown>;
    }>('/users/@me/settings', {
      signal: controller.signal
    })
      .then(({ theme, locale, notification_settings }) => {
        applyTheme(theme);
        applyLocale(locale);
        developerMode.apply(notification_settings);
      })
      .catch(() => {
        // The route's own session guard and error state handle unavailable APIs.
      });
    return () => {
      controller.abort();
      authenticatedGateway.stop();
      developerMode.reset();
    };
  });
</script>

{@render children()}
<CommandSwitcher />
