<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { entityRef } from '$lib/chat/refs';
  import { userDisplayName } from '$lib/chat/users';
  import { type Dispatch } from '$lib/gateway/client';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { directMessagePath } from '$lib/navigation/routes';
  import {
    browserNotifications,
    resolveNotificationPresence
  } from '$lib/notifications/browser.svelte';
  import { isNativeDesktop, nativeInvoke } from '$lib/platform/native';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { presenceControl } from '$lib/stores/presence.svelte';
  import { t } from '$lib/ui/locale';
  import { playCallSound } from '$lib/voice/call-sounds';

  let { ready }: { ready: boolean } = $props();
  interface Ring {
    id: string;
    acted_by_me?: boolean;
    state: string;
    authority_domain: string;
    channel_id: string;
    channel_domain: string;
    caller: string;
    created_at: number;
    participants: string[];
  }

  let ring = $state<Ring | null>(null);
  const user = $derived(chatEntities.currentUser);
  const quiet = $derived.by(() => {
    if (!user) return true;
    const presence = chatEntities.presenceFor(user);
    return (
      presenceControl.state?.suppress_notifications === true ||
      resolveNotificationPresence(presenceControl.state?.effective_status ?? null, presence) ===
        'dnd'
    );
  });
  const channel = $derived(
    ring && chatEntities.channels.get(`${ring.channel_id}@${ring.channel_domain}`)
  );
  const caller = $derived(
    ring &&
      (chatEntities.users.get(ring.caller) ??
        channel?.recipients?.find((recipient) => entityRef(recipient) === ring?.caller))
  );
  const title = $derived(
    ring && (channel?.type === 3 || ring.participants.length > 2)
      ? $t('call_group')
      : $t('call_incoming')
  );
  const detail = $derived(
    [caller && $t('call_caller', { name: userDisplayName(caller) }), channel?.name]
      .filter(Boolean)
      .join(' · ')
  );
  const path = $derived(
    ring && directMessagePath({ id: ring.channel_id, origin_domain: ring.channel_domain })
  );

  onMount(() => {
    const client = authenticatedGateway.client;
    let timeout: ReturnType<typeof setTimeout>;
    const receive = (event: Event) => {
      const { t: type, d } = (event as CustomEvent<Dispatch>).detail;
      if (!['CALL_RING', 'CALL_END', 'CALL_ACCEPT', 'CALL_DECLINE'].includes(type)) return;
      const call = d as Ring;
      const matches = ring?.id === call.id && ring.authority_domain === call.authority_domain;
      if (type !== 'CALL_RING') {
        if (
          matches &&
          (type === 'CALL_END' || call.state === 'ended' || call.acted_by_me !== false)
        )
          ring = null;
        return;
      }
      const remaining = Math.min(30000, call.created_at * 1000 + 30000 - Date.now());
      if (matches || remaining <= 0 || quiet || !user || call.caller === entityRef(user)) return;
      clearTimeout(timeout);
      ring = call;
      timeout = setTimeout(() => {
        ring = null;
      }, remaining);
    };
    client.addEventListener('dispatch', receive);
    return () => {
      clearTimeout(timeout);
      client.removeEventListener('dispatch', receive);
    };
  });

  $effect(() => {
    if (quiet) ring = null;
  });

  $effect(() => {
    if (!ready || !ring || quiet) return;
    const tone = browserNotifications.ringtone;
    const player = tone !== 'silent' && playCallSound(tone, true);
    return () => {
      if (player) player.pause();
    };
  });

  $effect(() => {
    if (!ready || !ring || quiet) return;
    return untrack(() => {
      if (document.hasFocus() && document.visibilityState === 'visible') return;
      if (browserNotifications.configured && !browserNotifications.enabled) return;
      if (isNativeDesktop()) {
        void nativeInvoke('native_notify', {
          title,
          body: detail,
          sensitive: false,
          deepLink: path
        }).catch(() => undefined);
      } else if ('Notification' in window && Notification.permission === 'granted') {
        const destination = path;
        const alert = new Notification(title, { body: detail, silent: true, tag: 'incoming-call' });
        alert.onclick = () => {
          window.focus();
          if (destination) window.location.assign(destination);
          alert.close();
        };
        return () => alert.close();
      }
    });
  });
</script>

{#if ready && ring && !quiet && path}
  <aside class="incoming-call" aria-label={title}>
    <div role="status">
      <strong>{title}</strong>{#if detail}<p>{detail}</p>{/if}
    </div>
    <div class="actions">
      <a
        class="primary-button"
        href={path}
        onclick={() => {
          ring = null;
        }}>{$t('call_open')}</a
      >
      <button
        class="secondary-button"
        onclick={() => {
          ring = null;
        }}>{$t('ui_dismiss_48845bff')}</button
      >
    </div>
  </aside>
{/if}

<style>
  .incoming-call {
    position: fixed;
    z-index: 150;
    inset: max(1rem, env(safe-area-inset-top)) 1rem auto auto;
    width: min(22rem, calc(100vw - 2rem));
    padding: 1rem;
    display: grid;
    gap: 0.9rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    background: var(--surface-raised);
    color: var(--text);
    box-shadow: var(--shadow-lg);
    overflow-wrap: anywhere;
  }
  p {
    color: var(--text-muted);
    margin: 0.25rem 0 0;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .actions > * {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
  }
</style>
