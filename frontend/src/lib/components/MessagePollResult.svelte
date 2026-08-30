<script lang="ts">
  import { partialEmojiText, type MessagePollResultPresentation } from '$lib/chat/rich-content';

  let { result }: { result: MessagePollResultPresentation } = $props();
  const winnerLabel = $derived(
    (result.victor_answer_text ?? partialEmojiText(result.victor_answer_emoji)) || null
  );
</script>

<section class="poll-result-card" aria-label="Poll results">
  <span class="poll-result-icon" aria-hidden="true">✓</span>
  <div>
    <strong>{result.question_text ?? 'Poll ended'}</strong>
    {#if result.total_votes === 0}
      <p>No votes were cast.</p>
    {:else if result.victor_answer_id === null}
      <p>The poll ended in a tie · {result.total_votes} votes</p>
    {:else}
      <p>
        {winnerLabel ?? `Answer ${result.victor_answer_id}`} won with
        {result.victor_answer_votes} of {result.total_votes} votes.
      </p>
    {/if}
  </div>
</section>

<style>
  .poll-result-card {
    display: flex;
    gap: 0.65rem;
    max-width: 32rem;
    margin-top: 0.35rem;
    padding: 0.7rem 0.8rem;
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 0.4rem;
    background: color-mix(in srgb, var(--panel) 86%, transparent);
  }

  .poll-result-icon {
    color: var(--accent);
    font-weight: 800;
  }

  p {
    margin: 0.2rem 0 0;
    color: var(--muted);
  }
</style>
