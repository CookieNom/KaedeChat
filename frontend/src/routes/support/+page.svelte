<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import LegalPage from '$lib/components/landing/LegalPage.svelte';
  import TurnstileWidget from '$lib/components/TurnstileWidget.svelte';

  let configuration = $state<{
    enabled: boolean;
    reasons: string[];
    turnstile: { enabled: boolean; site_key: string | null };
  } | null>(null);
  let loadFailed = $state(false);
  let email = $state('');
  let reason = $state('');
  let message = $state('');
  let busy = $state(false);
  let sent = $state(false);
  let error = $state('');
  let token = $state<string | null>(null);
  let widget = $state<TurnstileWidget | null>(null);
  let successPanel = $state<HTMLElement | null>(null);
  let canSubmit = $derived(
    configuration?.enabled &&
      !busy &&
      !sent &&
      email.trim() &&
      reason &&
      message.trim() &&
      (!configuration.turnstile.enabled || token)
  );

  onMount(() => {
    const controller = new AbortController();
    void api<NonNullable<typeof configuration>>('/support/config', { signal: controller.signal })
      .then((value) => (configuration = value))
      .catch(() => {
        if (!controller.signal.aborted) loadFailed = true;
      });
    return () => controller.abort();
  });

  async function submit() {
    if (!canSubmit) return;
    busy = true;
    error = '';
    try {
      await api('/support', {
        method: 'POST',
        body: JSON.stringify({ email, reason, message, turnstile_token: token })
      });
      sent = true;
      message = '';
      await tick();
      successPanel?.focus();
    } catch (caught) {
      error =
        caught instanceof ApiError && caught.status === 429
          ? 'Too many requests. Please wait before trying again.'
          : 'Your request could not be submitted. Please try again.';
      token = null;
      widget?.reset();
    } finally {
      busy = false;
    }
  }
</script>

<LegalPage title="Contact support" updated={null}>
  <div class="support-content">
    <p>Need help with this instance? Choose a reason and tell us what happened.</p>
    {#if loadFailed}
      <p class="form-error" role="alert">
        Support could not be loaded. Please reload this page to try again.
      </p>
    {:else if !configuration}
      <p role="status">Loading support options…</p>
    {:else if !configuration.enabled}
      <p role="status">The support form is currently unavailable. Please try again later.</p>
    {:else if sent}
      <div bind:this={successPanel} role="status" tabindex="-1" class="support-success">
        <h2>Request received</h2>
        <p>Your request has been submitted. Any reply will go to the email address you provided.</p>
        <a class="secondary-button" href={resolve('/')}>Back to home</a>
      </div>
    {:else}
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        aria-busy={busy}
      >
        <label>
          Your email address
          <input
            bind:value={email}
            type="email"
            autocomplete="email"
            maxlength="320"
            required
            disabled={busy}
          />
        </label>
        <label>
          Reason for contacting support
          <select bind:value={reason} required disabled={busy}>
            <option value="" disabled>Choose a reason</option>
            {#each configuration.reasons as option (option)}
              <option value={option}>{option}</option>
            {/each}
          </select>
        </label>
        <label>
          How can we help?
          <textarea
            bind:value={message}
            rows="7"
            maxlength="5000"
            required
            disabled={busy}
            aria-describedby="support-note"
          ></textarea>
        </label>
        <p id="support-note" class="support-note">
          Include relevant details, but never passwords or recovery codes. Your email address and
          message will be sent to this instance’s operator. <a href={resolve('/privacy')}
            >Privacy policy</a
          >
        </p>
        {#if configuration.turnstile.enabled && configuration.turnstile.site_key}
          <TurnstileWidget
            bind:this={widget}
            siteKey={configuration.turnstile.site_key}
            action="kaede-support"
            size="compact"
            onToken={(value) => (token = value)}
          />
          {#if !token}<p class="support-note">Complete the security check before sending.</p>{/if}
        {/if}
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <button class="primary-button" disabled={!canSubmit}
          >{busy ? 'Sending…' : 'Send support request'}</button
        >
      </form>
    {/if}
  </div>
</LegalPage>

<style>
  .support-content {
    max-width: 36rem;
  }
  form {
    display: grid;
    gap: 1.2rem;
    margin-top: 1.5rem;
  }
  .support-content .support-note {
    margin: 0;
    font-size: 0.875rem;
    color: var(--text-soft);
  }
  .support-success {
    margin-top: 1.5rem;
  }
  .support-success h2 {
    margin: 0;
  }
  .support-success a {
    margin-top: 1rem;
  }
  button {
    justify-self: start;
  }
  @media (max-width: 480px) {
    button {
      width: 100%;
    }
  }
</style>
