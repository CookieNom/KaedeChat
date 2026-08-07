<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  interface TurnstileApi {
    render: (element: HTMLElement, options: Record<string, unknown>) => string;
    reset: (widgetId: string) => void;
    remove: (widgetId: string) => void;
  }

  const SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  let {
    siteKey,
    action,
    onToken
  }: { siteKey: string; action: string; onToken: (token: string | null) => void } = $props();
  let container = $state<HTMLDivElement | null>(null);
  let widgetId: string | null = null;
  let cancelled = false;
  let loadFailed = $state(false);

  function turnstileApi(): TurnstileApi | undefined {
    return (window as Window & { turnstile?: TurnstileApi }).turnstile;
  }

  function loadScript(): Promise<void> {
    if (turnstileApi()) return Promise.resolve();
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT_URL}"]`);
    return new Promise((resolve, reject) => {
      const script = existing ?? document.createElement('script');
      script.addEventListener('load', () => resolve(), { once: true });
      script.addEventListener('error', () => reject(new Error('Turnstile failed to load')), {
        once: true
      });
      if (!existing) {
        script.src = SCRIPT_URL;
        script.async = true;
        script.defer = true;
        document.head.append(script);
      }
    });
  }

  export function reset() {
    onToken(null);
    if (widgetId) turnstileApi()?.reset(widgetId);
  }

  onMount(() => {
    void loadScript()
      .then(() => {
        const turnstile = turnstileApi();
        if (cancelled || !container || !turnstile) return;
        widgetId = turnstile.render(container, {
          sitekey: siteKey,
          action,
          theme: 'auto',
          callback: (token: string) => onToken(token),
          'expired-callback': () => onToken(null),
          'error-callback': () => onToken(null)
        });
      })
      .catch(() => {
        if (!cancelled) loadFailed = true;
      });
    return () => {
      cancelled = true;
    };
  });

  onDestroy(() => {
    if (widgetId) turnstileApi()?.remove(widgetId);
  });
</script>

<div class="turnstile-widget" bind:this={container}></div>
{#if loadFailed}
  <p class="form-error" role="alert">
    Verification could not load. Check your connection and reload.
  </p>
{/if}
