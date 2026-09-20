<script lang="ts">
  import { api } from '$lib/api/client';
  import { onMount, untrack } from 'svelte';

  type Request = {
    request_id: string;
    requester: { id: string; domain: string };
    requester_name: string;
    status: 'pending' | 'approved' | 'declined' | 'expired';
  };
  let {
    channelRef,
    userRef,
    refresh = 0,
    disabled = false,
    activationEnabled = true,
    onEnable,
    onStatus
  }: {
    channelRef: string;
    userRef: string;
    refresh?: number;
    disabled?: boolean;
    activationEnabled?: boolean;
    onEnable: () => Promise<void>;
    onStatus: (status: string | null) => void;
  } = $props();
  let request = $state<Request | null>(null);
  let busy = $state(false);
  let error = $state('');
  let stopped = false;
  let loading = false;
  const mine = $derived(
    request && `${request.requester.id}@${request.requester.domain}` === userRef
  );

  $effect(() => {
    onStatus(request?.status ?? null);
  });

  async function load() {
    if (loading || busy || stopped) return;
    loading = true;
    try {
      const result = await api<{ request: Request | null }>(
        `/e2ee/channels/${encodeURIComponent(channelRef)}/consent`,
        {
          method: 'POST',
          body: JSON.stringify({ action: 'status' })
        }
      );
      if (!stopped && !busy) request = result.request;
    } catch {
      // Keep the last request visible during temporary connection failures.
    } finally {
      loading = false;
    }
  }
  $effect(() => {
    void refresh;
    untrack(() => void load());
  });
  onMount(() => {
    const timer = setInterval(() => void load(), 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  });
  async function respond(action: 'agree' | 'disagree') {
    if (
      !request ||
      busy ||
      disabled ||
      mine ||
      request.status !== 'pending' ||
      (action === 'agree' && !activationEnabled)
    )
      return;
    busy = true;
    error = '';
    try {
      const result = await api<{ request: Request }>(
        `/e2ee/channels/${encodeURIComponent(channelRef)}/consent`,
        {
          method: 'POST',
          body: JSON.stringify({ action, request_id: request.request_id })
        }
      );
      if (stopped) return;
      request = result.request;
      if (action === 'agree') await onEnable();
    } catch {
      error = 'Could not update this encryption request. Please try again.';
    } finally {
      busy = false;
    }
  }
</script>

{#if request}
  <aside class="encryption-request" aria-label="Encryption request">
    <strong
      >{mine
        ? 'You requested end-to-end encryption'
        : `${request.requester_name} requested end-to-end encryption`}</strong
    >
    {#if request.status === 'pending' || request.status === 'approved'}
      <p>
        Only you and the other participant can read future encrypted messages. Earlier messages stay
        unencrypted. Once enabled, encryption cannot be turned off in this conversation.
      </p>
      <details>
        <summary>Read the warnings before agreeing</summary>
        <ul>
          <li>
            Server search, automatic link previews, and integrations without encrypted access stop
            working. Notifications may become generic.
          </li>
          <li>
            Encrypted files are not scanned by the server. Unsupported clients cannot use this
            conversation.
          </li>
          <li>
            Losing your key vault, trusted devices, and recovery backup means losing encrypted
            history.
          </li>
          <li>
            Servers can still see who communicates and when. Recipients can save or share messages.
            Compare your safety number through another trusted channel to verify identities.
          </li>
        </ul>
      </details>
      {#if request.status === 'approved'}
        <p role="status">You both agreed. Encryption still needs to finish turning on.</p>
        <button type="button" disabled={busy || disabled || !activationEnabled} onclick={onEnable}
          >Finish enabling encryption</button
        >
      {:else if mine}
        <p role="status">
          Waiting for the other participant to agree. Messages are still unencrypted.
        </p>
      {:else}
        <div class="actions">
          <button
            type="button"
            disabled={busy || disabled || !activationEnabled}
            onclick={() => respond('agree')}
            >{busy ? 'Please wait…' : 'Agree and enable encryption'}</button
          >
          <button
            type="button"
            class="secondary"
            disabled={busy || disabled}
            onclick={() => respond('disagree')}>Disagree</button
          >
        </div>
      {/if}
    {:else}
      <p role="status">
        {request.status === 'declined' ? 'The request was declined.' : 'This request expired.'} This conversation
        is still unencrypted.
      </p>
    {/if}
    {#if !activationEnabled && (request.status === 'pending' || request.status === 'approved')}
      <p>New encryption requests are currently disabled by the server.</p>
    {/if}
    {#if error}<p role="alert">{error}</p>{/if}
  </aside>
{/if}

<style>
  .encryption-request {
    margin: 12px 16px;
    padding: 16px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface-raised);
    max-height: 40vh;
    overflow-y: auto;
  }
  p,
  details {
    margin: 10px 0;
    font-size: 0.875rem;
    line-height: 1.5;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
  }
  ul {
    padding-left: 20px;
  }
  li + li {
    margin-top: 6px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
  }
  button {
    min-height: 44px;
    padding: 10px 16px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--accent);
    color: var(--on-accent);
    cursor: pointer;
    font-weight: 600;
  }
  button.secondary {
    background: var(--surface-raised);
    color: var(--text);
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  @media (max-width: 600px) {
    .encryption-request {
      margin: 8px;
      padding: 12px;
    }
  }
</style>
