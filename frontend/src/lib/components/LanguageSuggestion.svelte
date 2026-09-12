<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import {
    activeLocale,
    applyLocale,
    languagePromptHandled,
    languages,
    markLanguageChosen,
    suggestedLanguage,
    systemLanguages,
    translate
  } from '$lib/ui/locale';
  let candidate = $state<string>();
  let handled = $state(false);
  let busy = $state(false);
  let error = $state(false);
  const name = $derived(languages.find((item) => item.code === candidate)?.name ?? '');
  onMount(() => {
    handled = languagePromptHandled();
    const check = () => {
      candidate = suggestedLanguage(systemLanguages(), $activeLocale, handled);
    };
    check();
    const chosen = () => {
      handled = true;
    };
    window.addEventListener('kaede:language-choice', chosen);
    window.addEventListener('languagechange', check);
    window.addEventListener('kaede:system-language', check);
    return () => {
      window.removeEventListener('kaede:language-choice', chosen);
      window.removeEventListener('languagechange', check);
      window.removeEventListener('kaede:system-language', check);
    };
  });
  function dismiss() {
    markLanguageChosen();
    handled = true;
  }
  async function accept() {
    if (!candidate || busy) return;
    busy = true;
    error = false;
    try {
      const updated = await api<{ locale: string }>('/users/@me/settings', {
        method: 'PATCH',
        body: JSON.stringify({ locale: 'system' })
      });
      applyLocale(updated.locale);
      dismiss();
    } catch {
      error = true;
    } finally {
      busy = false;
    }
  }
</script>

{#if candidate && !handled && candidate !== $activeLocale}
  <aside
    class="language-suggestion"
    aria-label={`${translate('language_settings', {}, 'en')} / ${translate('language_settings', {}, candidate)}`}
  >
    <p lang="en" dir="ltr">{translate('language_suggestion', { language: name }, 'en')}</p>
    <p lang={candidate} dir={/^(ar|fa|he|ur)(-|$)/.test(candidate) ? 'rtl' : 'ltr'}>
      {translate('language_suggestion', { language: name }, candidate)}
    </p>
    {#if error}
      <div role="alert">
        <p lang="en">{translate('language_save_error', {}, 'en')}</p>
        <p lang={candidate}>{translate('language_save_error', {}, candidate)}</p>
      </div>
    {/if}
    <div class="actions">
      <button
        class="decline"
        disabled={busy}
        onclick={dismiss}
        aria-label={`${translate('language_decline', {}, 'en')} / ${translate('language_decline', {}, candidate)}`}
        ><span aria-hidden="true">✕</span></button
      >
      <button
        class="accept"
        disabled={busy}
        onclick={() => void accept()}
        aria-label={`${translate('language_accept', { language: name }, 'en')} / ${translate('language_accept', { language: name }, candidate)}`}
        ><span aria-hidden="true">✓</span></button
      >
    </div>
  </aside>
{/if}

<style>
  .language-suggestion {
    position: fixed;
    z-index: 130;
    inset-inline-end: 18px;
    top: 72px;
    width: min(420px, calc(100vw - 36px));
    max-height: calc(100dvh - 100px);
    overflow: auto;
    padding: 20px;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface-raised);
    color: var(--text);
    box-shadow: var(--shadow-lg);
  }
  p {
    margin: 0 0 12px;
    line-height: 1.5;
  }
  .actions {
    display: flex;
    gap: 12px;
    justify-content: flex-end;
  }
  button {
    min-width: 48px;
    min-height: 48px;
    border: 0;
    border-radius: 10px;
    color: white;
    font-size: 24px;
    cursor: pointer;
  }
  .decline {
    background: #b42318;
  }
  .accept {
    background: #157347;
  }
  button:focus-visible {
    outline: 3px solid var(--text);
    outline-offset: 3px;
  }
  button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
</style>
