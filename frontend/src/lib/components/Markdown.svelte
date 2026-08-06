<script lang="ts">
  import { renderMessageMarkdown } from '$lib/chat/markdown';
  import type { UserSummary } from '$lib/chat/types';

  let { content, mentionUsers = [] }: { content: string; mentionUsers?: UserSummary[] } = $props();
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname;
  const rendered = $derived(renderMessageMarkdown(content, mentionUsers, localDomain));

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
  class="message-markdown"
  role="group"
  aria-label="Message content"
  onclick={(event) => activate(event.target)}
  onkeydown={keydown}
>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -- renderMessageMarkdown applies a strict DOMPurify allowlist before token decoration -->
  {@html rendered}
</div>
