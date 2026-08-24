<script lang="ts">
  import { renderMessageMarkdown } from '$lib/chat/markdown';
  import type { Role, UserSummary } from '$lib/chat/types';
  import { tick } from 'svelte';

  let {
    content,
    mentionUsers = [],
    mentionRoles = []
  }: { content: string; mentionUsers?: UserSummary[]; mentionRoles?: Role[] } = $props();
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname;
  const rendered = $derived(
    renderMessageMarkdown(content, mentionUsers, localDomain, mentionRoles)
  );
  let container = $state<HTMLDivElement | null>(null);

  $effect(() => {
    const currentRendered = rendered;
    void tick().then(() => {
      if (!container || rendered !== currentRendered) return;
      for (const mention of container.querySelectorAll<HTMLElement>(
        '.chat-token-role-mention[data-role-color]'
      )) {
        const color = mention.dataset.roleColor;
        if (/^#[0-9a-f]{6}$/i.test(color ?? '')) {
          // Property-level CSSOM updates work with Kaede's strict style CSP;
          // serialized inline style attributes intentionally do not.
          mention.style.setProperty('--mention-role-color', color!);
        }
      }
    });
  });

  function activate(target: EventTarget | null) {
    if (!(target instanceof Element)) return;
    const spoiler = target.closest<HTMLElement>('.chat-spoiler');
    if (spoiler) {
      const revealed = spoiler.classList.toggle('revealed');
      spoiler.setAttribute('aria-label', revealed ? 'Hide spoiler' : 'Reveal spoiler');
      return;
    }
    const mention = target.closest<HTMLElement>('[data-user-handle], [data-user-ref]');
    if (mention) {
      window.dispatchEvent(
        new CustomEvent('kaede:open-user-profile', {
          detail: { handle: mention.dataset.userHandle, reference: mention.dataset.userRef }
        })
      );
    }
  }

  function keydown(event: KeyboardEvent) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest('.chat-spoiler, [data-user-handle], [data-user-ref]')) return;
    event.preventDefault();
    activate(event.target);
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions (interaction is delegated only to keyboard-accessible spoiler and mention descendants created by the sanitizer) -->
<div
  bind:this={container}
  class="message-markdown"
  role="group"
  aria-label="Message content"
  onclick={(event) => activate(event.target)}
  onkeydown={keydown}
>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -- renderMessageMarkdown applies a strict DOMPurify allowlist before token decoration -->
  {@html rendered}
</div>
