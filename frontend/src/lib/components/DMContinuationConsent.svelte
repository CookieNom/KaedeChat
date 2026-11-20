<script lang="ts">
  import { t } from '$lib/ui/locale';
  import { onMount, untrack } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';

  let {
    room,
    account,
    canRequest = $bindable(false),
    notice,
    callActive = false,
    onContinue
  }: {
    room: string;
    account: string;
    canRequest?: boolean;
    notice?: string | null;
    callActive?: boolean;
    onContinue: (ref: string) => void;
  } = $props();
  interface Proposal {
    request_id: string;
    status: string;
    requester?: string;
    requester_name?: string;
    approved_by: string[];
    context: { participants: string[] };
  }
  interface Response {
    request?: Proposal | null;
    channel_ref?: string;
    available?: boolean;
  }
  let proposal = $state<Proposal | null>(null);
  let available = $state(false);
  let busy = $state(false);
  let error = $state('');
  let disposed = false;
  let confirmation: HTMLDialogElement;
  let confirming = $state<'request' | 'agree'>('request');
  let confirmedId: string | undefined;
  let declined = $state(false);
  export function request() {
    promptVote('request');
  }
  function promptVote(action: 'request' | 'agree') {
    if (!available || busy || (action === 'request' && awaiting)) return;
    confirmedId = proposal?.request_id;
    confirming = action;
    confirmation.showModal();
  }
  $effect(() => {
    void notice;
    untrack(() => void load());
  });
  let reading = false;
  let revision = 0;
  const path = $derived('/e2ee/channels/' + encodeURIComponent(room) + '/plaintext-continuation/');
  const awaiting = $derived(proposal?.status === 'pending' || proposal?.status === 'approved');
  const mine = $derived(proposal?.approved_by.includes(account));
  $effect(() => {
    canRequest = available && !awaiting && !busy;
  });
  function update(result: Response) {
    if (result.channel_ref) {
      onContinue(result.channel_ref);
      return;
    }
    available = result.available !== false;
    if (result.request?.status === 'declined' && awaiting) declined = true;
    else if (result.request?.status !== 'declined') declined = false;
    proposal = result.request ?? null;
  }
  async function load() {
    if (reading || busy || disposed) return;
    reading = true;
    const started = revision;
    try {
      const result = await api<Response>(path + 'consent', {
        method: 'POST',
        body: JSON.stringify({ action: 'status' })
      });
      if (!disposed && started === revision) update(result);
    } catch {
      /* A disconnected client retains its last confirmed vote. */
    } finally {
      reading = false;
    }
  }
  onMount(() => {
    const timer = setInterval(() => void load(), 5000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  });
  async function act(action: 'request' | 'agree' | 'disagree' | 'commit') {
    if (disposed || busy || !available || (action !== 'request' && !proposal)) return;
    if (action === 'commit' && callActive) return;
    busy = true;
    revision++;
    error = '';
    try {
      let result = await api<Response>(path + (action === 'commit' ? 'commit' : 'consent'), {
        method: 'POST',
        body: JSON.stringify(
          action === 'commit'
            ? { request_id: proposal!.request_id }
            : { action, request_id: proposal?.request_id }
        )
      });
      if (disposed) return;
      update(result);
      if (result.request?.status === 'approved' && !callActive) {
        result = await api<Response>(path + 'commit', {
          method: 'POST',
          body: JSON.stringify({ request_id: result.request.request_id })
        });
        if (!disposed) update(result);
      }
    } catch (e) {
      if (!disposed) error = userErrorMessage(e, $t('dm_privacy_error'));
    } finally {
      busy = false;
    }
  }
</script>

<dialog class="action-dialog" bind:this={confirmation}>
  <form method="dialog">
    <h2>{$t('dm_privacy_question')}</h2>
    <p>{$t('dm_privacy_explain')}</p>
    <footer>
      <button>{$t('dm_privacy_cancel')}</button>
      <button
        class="primary"
        onclick={() => {
          if (confirmedId === proposal?.request_id && (confirming !== 'request' || !awaiting))
            void act(confirming);
        }}>{confirming === 'request' ? $t('dm_privacy_send') : $t('dm_privacy_agree')}</button
      >
    </footer>
  </form>
</dialog>
{#if available && !awaiting && (declined || error)}
  <div class="privacy" role="status">
    {error || $t('dm_privacy_declined')}
    <button
      type="button"
      onclick={() => {
        declined = false;
        error = '';
      }}>{$t('ui_close_7d9eb7ac')}</button
    >
  </div>
{:else if available && awaiting && proposal}
  <details open class="privacy">
    <summary
      >{$t('dm_privacy_asked', {
        name: proposal.requester_name ?? proposal.requester ?? proposal.approved_by[0] ?? ''
      })}</summary
    >
    <p>
      {$t('dm_privacy_explain')}
    </p>
    <p role="status">
      {$t('dm_privacy_votes', {
        approved: String(proposal.approved_by.length),
        total: String(proposal.context.participants.length)
      })}
    </p>
    {#if proposal.status === 'approved' && callActive}<p>
        {$t('dm_privacy_end_call')}
      </p>{/if}
    {#if proposal.status === 'approved'}
      <button
        type="button"
        class="primary"
        disabled={busy || callActive}
        onclick={() => act('commit')}>{$t('dm_privacy_finish')}</button
      >
    {:else if !mine}
      <button type="button" class="primary" disabled={busy} onclick={() => promptVote('agree')}
        >{$t('dm_privacy_agree')}</button
      >
    {/if}
    <button type="button" disabled={busy} onclick={() => act('disagree')}
      >{mine ? $t('dm_privacy_withdraw') : $t('dm_privacy_decline')}</button
    >
    {#if error}<p role="alert">{error}</p>{/if}
  </details>
{/if}

<style>
  .privacy {
    flex: none;
    padding: 0.45rem 1rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    max-height: 32dvh;
    overflow: auto;
  }
  summary {
    cursor: pointer;
    font-weight: 650;
  }
  p {
    margin: 0.5rem 0;
  }
  button {
    border: 1px solid var(--border);
    background: var(--surface-raised);
    border-radius: 0.5rem;
    color: inherit;
    min-height: 2.75rem;
    padding: 0.4rem 0.8rem;
    margin: 0 0.4rem 0.25rem 0;
  }
  button.primary {
    background: var(--accent);
    color: var(--on-accent);
  }
  button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
</style>
