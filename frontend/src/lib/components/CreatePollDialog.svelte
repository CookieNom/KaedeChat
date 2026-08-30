<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import type { CustomEmojiOption } from '$lib/chat/emojis';
  import type { PartialEmoji, PollCreatePayload } from '$lib/chat/rich-content';
  import { portal } from '$lib/ui/portal';
  import { onMount, tick } from 'svelte';
  import EmojiPicker from './EmojiPicker.svelte';
  import PartialEmojiView from './PartialEmoji.svelte';

  let {
    customEmojis = [],
    onCreate,
    onClose
  }: {
    customEmojis?: CustomEmojiOption[];
    onCreate: (poll: PollCreatePayload) => Promise<void>;
    onClose: () => void;
  } = $props();

  interface AnswerDraft {
    text: string;
    emoji: PartialEmoji | null;
  }

  const durations = [1, 4, 8, 24, 72, 168] as const;

  let question = $state('');
  let answers = $state<AnswerDraft[]>([
    { text: '', emoji: null },
    { text: '', emoji: null }
  ]);
  let duration = $state(24);
  let allowMultiselect = $state(false);
  let busy = $state(false);
  let error = $state('');
  let questionInput = $state<HTMLInputElement | null>(null);
  let emojiAnswerIndex = $state<number | null>(null);

  const complete = $derived(
    Boolean(
      question.trim() &&
      answers.length >= 2 &&
      answers.length <= 10 &&
      answers.every((answer) => answer.text.trim() || answer.emoji) &&
      durations.includes(duration as (typeof durations)[number])
    )
  );

  onMount(() => void tick().then(() => questionInput?.focus()));

  function updateAnswer(index: number, value: string) {
    answers = answers.map((answer, answerIndex) =>
      answerIndex === index ? { ...answer, text: value } : answer
    );
  }

  function chooseEmoji(index: number, value: string) {
    const custom = customEmojis.find((emoji) => emoji.value === value);
    const emoji: PartialEmoji = custom
      ? {
          id: `${custom.id}@${custom.origin_domain}`,
          name: custom.name,
          animated: custom.animated ?? false
        }
      : { name: value, animated: false };
    answers = answers.map((answer, answerIndex) =>
      answerIndex === index ? { ...answer, emoji } : answer
    );
    emojiAnswerIndex = null;
  }

  function chooseOpenEmoji(value: string) {
    if (emojiAnswerIndex !== null) chooseEmoji(emojiAnswerIndex, value);
  }

  async function submit() {
    if (!complete || busy) return;
    busy = true;
    error = '';
    try {
      await onCreate({
        question: { text: question.trim() },
        answers: answers.map((answer) => ({
          poll_media: {
            ...(answer.text.trim() ? { text: answer.text.trim() } : {}),
            ...(answer.emoji ? { emoji: answer.emoji } : {})
          }
        })),
        duration,
        allow_multiselect: allowMultiselect,
        layout_type: 1
      });
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the poll. Try again.');
    } finally {
      busy = false;
    }
  }
</script>

<div
  use:portal
  class="poll-dialog-layer"
  role="presentation"
  onkeydown={(event) => {
    if (event.key === 'Escape' && !busy) onClose();
  }}
>
  <button class="poll-dialog-backdrop" type="button" aria-label="Cancel poll" onclick={onClose}
  ></button>
  <form
    class="poll-dialog"
    aria-label="Create poll"
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <header>
      <div>
        <small>New message</small>
        <h2>Create a poll</h2>
      </div>
      <button type="button" aria-label="Close" disabled={busy} onclick={onClose}>×</button>
    </header>
    <label>
      Question
      <input bind:this={questionInput} bind:value={question} maxlength="300" required />
    </label>
    <fieldset>
      <legend>Answers</legend>
      {#each answers as answer, index (index)}
        <div class="poll-answer-field">
          <button
            class="answer-emoji"
            class:selected={Boolean(answer.emoji)}
            type="button"
            aria-label={`Choose emoji for answer ${index + 1}`}
            aria-expanded={emojiAnswerIndex === index}
            onclick={() => (emojiAnswerIndex = emojiAnswerIndex === index ? null : index)}
          >
            {#if answer.emoji}
              <PartialEmojiView emoji={answer.emoji} size={20} />
            {:else}
              <span aria-hidden="true">☺</span>
            {/if}
          </button>
          <input
            value={answer.text}
            maxlength="55"
            aria-label={`Answer ${index + 1}`}
            oninput={(event) => updateAnswer(index, event.currentTarget.value)}
          />
          {#if answer.emoji}
            <button
              class="remove-emoji"
              type="button"
              aria-label={`Remove emoji from answer ${index + 1}`}
              onclick={() =>
                (answers = answers.map((item, answerIndex) =>
                  answerIndex === index ? { ...item, emoji: null } : item
                ))}>×</button
            >
          {/if}
          {#if answers.length > 2}
            <button
              type="button"
              aria-label={`Remove answer ${index + 1}`}
              onclick={() => (answers = answers.filter((_, answerIndex) => answerIndex !== index))}
              >×</button
            >
          {/if}
        </div>
      {/each}
      {#if answers.length < 10}
        <button
          type="button"
          class="add-answer"
          onclick={() => (answers = [...answers, { text: '', emoji: null }])}>Add answer</button
        >
      {/if}
      {#if emojiAnswerIndex !== null}
        <div class="poll-emoji-picker">
          <EmojiPicker
            {customEmojis}
            onSelect={chooseOpenEmoji}
            onClose={() => (emojiAnswerIndex = null)}
          />
        </div>
      {/if}
    </fieldset>
    <div class="poll-options">
      <label>
        Duration
        <select bind:value={duration} required>
          <option value={1}>1 hour</option>
          <option value={4}>4 hours</option>
          <option value={8}>8 hours</option>
          <option value={24}>24 hours</option>
          <option value={72}>3 days</option>
          <option value={168}>1 week</option>
        </select>
      </label>
      <label class="poll-checkbox">
        <input type="checkbox" bind:checked={allowMultiselect} />
        Allow multiple answers
      </label>
    </div>
    {#if error}<p role="alert">{error}</p>{/if}
    <footer>
      <button type="button" disabled={busy} onclick={onClose}>Cancel</button>
      <button class="primary" type="submit" disabled={busy || !complete}
        >{busy ? 'Creating…' : 'Create poll'}</button
      >
    </footer>
  </form>
</div>

<style>
  .poll-dialog-layer {
    position: fixed;
    z-index: 4000;
    inset: 0;
    display: grid;
    place-items: center;
    padding: 1rem;
  }
  .poll-dialog-backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: rgb(0 0 0 / 58%);
  }
  .poll-dialog {
    position: relative;
    width: min(560px, 100%);
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1rem;
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: 0 24px 80px rgb(0 0 0 / 45%);
  }
  header,
  footer,
  .poll-answer-field,
  .poll-options {
    display: flex;
    align-items: center;
    gap: 0.7rem;
  }
  header,
  footer {
    justify-content: space-between;
  }
  header h2,
  header small {
    margin: 0;
  }
  label,
  fieldset {
    display: grid;
    gap: 0.35rem;
    margin-top: 0.85rem;
    border: 0;
    padding: 0;
    font-size: 0.78rem;
    font-weight: 700;
  }
  input,
  select {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.65rem;
    color: var(--text);
    background: var(--surface-subtle);
  }
  .poll-answer-field input {
    flex: 1;
  }
  .answer-emoji,
  .remove-emoji {
    flex: 0 0 auto;
    min-width: 38px;
    padding: 0.45rem;
  }
  .answer-emoji.selected {
    border-color: var(--accent);
  }
  .poll-emoji-picker {
    position: relative;
    z-index: 2;
    margin-top: 0.5rem;
  }
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    color: var(--text);
    background: var(--surface-subtle);
    cursor: pointer;
  }
  button:disabled {
    cursor: default;
    opacity: 0.55;
  }
  .add-answer {
    justify-self: start;
  }
  .poll-options > label {
    flex: 1;
  }
  .poll-checkbox {
    display: flex;
    align-items: center;
    margin-top: 1.8rem;
  }
  .poll-checkbox input {
    width: auto;
  }
  footer {
    margin-top: 1rem;
  }
  .primary {
    border-color: var(--accent);
    background: var(--accent);
    color: white;
  }
  p {
    color: var(--danger);
  }
</style>
