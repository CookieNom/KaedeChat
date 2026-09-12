<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { userErrorMessage } from '$lib/api/client';
  import type { MessagePoll as Poll } from '$lib/chat/rich-content';
  import {
    partialEmojiText,
    pollAnswerPercent,
    pollCount,
    pollIsClosed,
    pollTotalVotes
  } from '$lib/chat/rich-content';
  import { finalizePoll, listPollVoters, setPollVote } from '$lib/chat/interactions';
  import type { Message, UserSummary } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';

  let {
    poll,
    channelRef,
    messageRef,
    disabled = false,
    readOnly = false,
    canClose = false,
    onUpdated,
    onVote,
    onLoadVoters,
    onClosePoll
  }: {
    poll: Poll;
    channelRef: string;
    messageRef: string;
    disabled?: boolean;
    readOnly?: boolean;
    canClose?: boolean;
    onUpdated?: (message: Message) => void;
    onVote?: (answerId: number, selected: boolean) => Promise<void>;
    onLoadVoters?: (
      answerId: number,
      after?: string | null
    ) => Promise<{
      users: UserSummary[];
      next_after: string | null;
    }>;
    onClosePoll?: () => Promise<Message | void>;
  } = $props();

  let busyAnswer = $state<number | null>(null);
  let error = $state('');
  let closing = $state(false);
  let votersDialog = $state<HTMLDialogElement | null>(null);
  let votersAnswer = $state<number | null>(null);
  let voters = $state<UserSummary[]>([]);
  let votersAfter = $state<string | null>(null);
  let votersLoading = $state(false);
  let votersError = $state('');
  const closed = $derived(pollIsClosed(poll));
  const total = $derived(pollTotalVotes(poll));

  async function toggle(answerId: number) {
    if (busyAnswer !== null || disabled || readOnly || closed) return;
    const count = pollCount(poll, answerId);
    busyAnswer = answerId;
    error = '';
    try {
      if (onVote) await onVote(answerId, !count.me_voted);
      else await setPollVote(channelRef, messageRef, answerId, !count.me_voted);
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_update_your_vote_try_again_360560d2'));
    } finally {
      busyAnswer = null;
    }
  }

  function expiryLabel(): string {
    const date = new Date(poll.expiry);
    if (Number.isNaN(date.getTime())) return closed ? 'Poll closed' : 'Poll';
    return closed
      ? `Poll closed · ${date.toLocaleString()}`
      : `Voting ends ${date.toLocaleString()}`;
  }

  async function closePoll() {
    if (closing || disabled || closed || !canClose) return;
    closing = true;
    error = '';
    try {
      const updated = onClosePoll
        ? await onClosePoll()
        : await finalizePoll(channelRef, messageRef);
      if (updated) onUpdated?.(updated);
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_close_this_poll_try_again_2651c554'));
    } finally {
      closing = false;
    }
  }

  async function loadVoters(append = false) {
    const answerId = votersAnswer;
    if (answerId === null || votersLoading) return;
    votersLoading = true;
    votersError = '';
    try {
      const page = onLoadVoters
        ? await onLoadVoters(answerId, append ? votersAfter : null)
        : await listPollVoters(channelRef, messageRef, answerId, append ? votersAfter : null);
      voters = append ? [...voters, ...page.users] : page.users;
      votersAfter = page.next_after;
    } catch (caught) {
      votersError = userErrorMessage(caught, $t('ui_could_not_load_voters_try_again_d316bbdc'));
    } finally {
      votersLoading = false;
    }
  }

  function showVoters(answerId: number) {
    votersAnswer = answerId;
    voters = [];
    votersAfter = null;
    votersError = '';
    votersDialog?.showModal();
    void loadVoters();
  }
</script>

<section
  class="message-poll"
  aria-label={`Poll: ${poll.question.text ?? $t('ui_question_289aff12')}`}
>
  <h3>{poll.question.text ?? $t('ui_poll_d54f7d12')}</h3>
  <div class="poll-answers">
    {#each poll.answers as answer (answer.answer_id)}
      {@const result = pollCount(poll, answer.answer_id)}
      {@const percent = pollAnswerPercent(poll, answer.answer_id)}
      <div class="poll-answer-row">
        <button
          type="button"
          class:selected={result.me_voted}
          class="poll-vote"
          disabled={disabled || readOnly || closed || busyAnswer !== null}
          aria-pressed={result.me_voted}
          aria-label={`${answer.poll_media.text ?? partialEmojiText(answer.poll_media.emoji)}, ${result.count} vote${result.count === 1 ? '' : 's'}, ${percent} percent${result.me_voted ? $t('ui_selected_bd44f3ef') : ''}`}
          onclick={() => void toggle(answer.answer_id)}
        >
          <span class="poll-fill" style:width={`${percent}%`} aria-hidden="true"></span>
          <span class="poll-choice">
            {#if answer.poll_media.emoji}<span>{partialEmojiText(answer.poll_media.emoji)}</span
              >{/if}
            <strong>{answer.poll_media.text ?? $t('ui_option_45aaacba')}</strong>
          </span>
          <span class="poll-result">{result.count} · {percent}%</span>
        </button>
        {#if result.count > 0 && !readOnly}
          <button
            type="button"
            class="poll-voters"
            {disabled}
            aria-label={`View voters for ${answer.poll_media.text ?? $t('ui_this_option_9c8fff5c')}`}
            onclick={() => showVoters(answer.answer_id)}>{$t('ui_voters_14fce270')}</button
          >
        {/if}
      </div>
    {/each}
  </div>
  <footer>
    <span
      >{$t('ui_value0_vote_value1_5f6c4926', {
        value0: String(total),
        value1: String(total === 1 ? '' : 's')
      })}</span
    >
    <span aria-hidden="true">•</span>
    {#if readOnly}
      <span>{$t('ui_private_poll_voting_is_unavailable_in_private_59ccc065')}</span>
    {:else}
      <span
        >{poll.allow_multiselect
          ? $t('ui_choose_one_or_more_97169869')
          : $t('ui_choose_one_0b24aed5')}</span
      >
      <span aria-hidden="true">•</span>
      <span>{expiryLabel()}</span>
    {/if}
    {#if canClose && !readOnly && !closed}
      <button
        type="button"
        class="poll-close"
        disabled={disabled || closing}
        onclick={() => void closePoll()}
        >{closing ? $t('ui_closing_abdb0cfd') : $t('ui_close_poll_f6740d64')}</button
      >
    {/if}
  </footer>
  {#if error}<p role="alert">{error}</p>{/if}
</section>

<dialog bind:this={votersDialog} class="poll-voters-dialog">
  <div class="poll-voters-heading">
    <div>
      <small>{$t('ui_poll_voters_876a1b78')}</small>
      <h3>
        {poll.answers.find((answer) => answer.answer_id === votersAnswer)?.poll_media.text ??
          $t('ui_answer_b2a3aa60')}
      </h3>
    </div>
    <button
      type="button"
      aria-label={$t('ui_close_voter_list_c8e4ceda')}
      onclick={() => votersDialog?.close()}>×</button
    >
  </div>
  {#if voters.length}
    <ul>
      {#each voters as user (`${user.id}@${user.origin_domain}`)}
        <li><strong>{userDisplayName(user)}</strong><span>@{user.handle}</span></li>
      {/each}
    </ul>
  {:else if !votersLoading && !votersError}
    <p>{$t('ui_no_voters_are_visible_for_this_answer_5b849dde')}</p>
  {/if}
  {#if votersError}<p role="alert">{votersError}</p>{/if}
  <footer>
    {#if votersAfter}
      <button type="button" disabled={votersLoading} onclick={() => void loadVoters(true)}
        >{votersLoading ? $t('ui_loading_ba3bbbe1') : $t('ui_load_more_ac8991ef')}</button
      >
    {:else if votersLoading}
      <span>{$t('ui_loading_voters_c7cc036f')}</span>
    {/if}
    <button type="button" onclick={() => votersDialog?.close()}>{$t('ui_done_11a6767d')}</button>
  </footer>
</dialog>

<style>
  .message-poll {
    width: min(520px, 100%);
    margin-top: 9px;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px;
    background: var(--surface-raised);
  }
  h3 {
    margin: 0 0 10px;
    font-size: 0.96rem;
  }
  .poll-answers {
    display: grid;
    gap: 7px;
  }
  .poll-vote {
    position: relative;
    display: flex;
    min-height: 42px;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 8px 10px;
    color: var(--text);
    background: var(--surface-subtle);
    text-align: left;
    cursor: pointer;
  }
  .poll-vote:hover:not(:disabled),
  .poll-vote.selected {
    border-color: var(--accent);
  }
  .poll-vote:disabled {
    cursor: default;
    opacity: 0.8;
  }
  .poll-fill {
    position: absolute;
    inset: 0 auto 0 0;
    max-width: 100%;
    background: color-mix(in srgb, var(--accent) 15%, transparent);
    transition: width 180ms ease;
  }
  .poll-choice,
  .poll-result {
    position: relative;
  }
  .poll-choice {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 7px;
  }
  .poll-result {
    flex: 0 0 auto;
    color: var(--text-muted);
    font-size: 0.76rem;
    font-weight: 700;
  }
  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 9px;
    color: var(--text-muted);
    font-size: 0.72rem;
  }
  .poll-answer-row {
    display: flex;
    align-items: stretch;
    gap: 5px;
  }
  .poll-vote {
    flex: 1;
  }
  .poll-voters,
  .poll-close,
  .poll-voters-dialog button {
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.4rem 0.6rem;
    color: var(--text);
    background: var(--surface-subtle);
    cursor: pointer;
  }
  .poll-voters {
    flex: 0 0 auto;
    font-size: 0.7rem;
  }
  .poll-close {
    margin-left: auto;
    color: var(--danger);
  }
  .poll-voters-dialog {
    width: min(430px, calc(100vw - 2rem));
    max-height: min(560px, calc(100vh - 2rem));
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
    color: var(--text);
    background: var(--surface-raised);
  }
  .poll-voters-dialog::backdrop {
    background: rgb(0 0 0 / 55%);
  }
  .poll-voters-heading,
  .poll-voters-dialog footer,
  .poll-voters-dialog li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .poll-voters-heading h3,
  .poll-voters-heading small {
    margin: 0;
  }
  .poll-voters-dialog ul {
    display: grid;
    gap: 0.45rem;
    margin: 1rem 0;
    padding: 0;
    list-style: none;
  }
  .poll-voters-dialog li {
    border-bottom: 1px solid var(--line);
    padding: 0.5rem 0;
  }
  .poll-voters-dialog li span {
    color: var(--text-muted);
    font-size: 0.75rem;
  }
  p {
    margin: 8px 0 0;
    color: var(--danger);
    font-size: 0.78rem;
  }
</style>
