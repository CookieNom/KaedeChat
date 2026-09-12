<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { partialEmojiText, type MessagePollResultPresentation } from '$lib/chat/rich-content';

  let { result }: { result: MessagePollResultPresentation } = $props();
  const winnerLabel = $derived(
    (result.victor_answer_text ?? partialEmojiText(result.victor_answer_emoji)) || null
  );
</script>

<section class="poll-result-card" aria-label={$t('ui_poll_results_ac41cda4')}>
  <span class="poll-result-icon" aria-hidden="true">✓</span>
  <div>
    <strong>{result.question_text ?? $t('ui_poll_ended_f5031fd1')}</strong>
    {#if result.total_votes === 0}
      <p>{$t('ui_no_votes_were_cast_b293652a')}</p>
    {:else if result.victor_answer_id === null}
      <p>
        {$t('ui_the_poll_ended_in_a_tie_value0_votes_ac571d9f', {
          value0: String(result.total_votes)
        })}
      </p>
    {:else}
      <p>
        {$t('ui_value0_won_with_value1_of_value2_votes_58ca41de', {
          value0: String(winnerLabel ?? `Answer ${result.victor_answer_id}`),
          value1: String(result.victor_answer_votes),
          value2: String(result.total_votes)
        })}
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
