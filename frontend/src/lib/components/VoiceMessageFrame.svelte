<script lang="ts">
  import type { Snippet } from 'svelte';
  import { voiceDurationLabel, voiceWaveformSamples } from '$lib/chat/voice-messages';
  let {
    duration,
    waveform,
    encrypted = false,
    children
  }: {
    duration: number | null | undefined;
    waveform: string | null | undefined;
    encrypted?: boolean;
    children: Snippet;
  } = $props();
  const samples = $derived(voiceWaveformSamples(waveform));
</script>

<div
  class="voice-message"
  aria-label={`${encrypted ? 'Encrypted voice message' : 'Voice message'} · ${voiceDurationLabel(duration)}`}
>
  <div class="voice-heading">
    <span aria-hidden="true">{encrypted ? '🔒🎙️' : '🎙️'}</span>
    <strong>Voice message</strong>
    <small>{voiceDurationLabel(duration)}</small>
  </div>
  {#if samples.length}
    <div class="voice-waveform" aria-hidden="true">
      {#each samples as sample, index (index)}
        <i style={`--voice-sample: ${sample}`}></i>
      {/each}
    </div>
  {/if}
  {@render children()}
</div>

<style>
  .voice-message {
    display: grid;
    width: min(430px, 100%);
    gap: 0.45rem;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.65rem 0.75rem;
    background: var(--surface-subtle);
  }

  .voice-heading {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--text-soft);
    font-size: 0.76rem;
  }

  .voice-heading small {
    margin-left: auto;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }

  .voice-waveform {
    display: flex;
    height: 30px;
    align-items: center;
    gap: 1px;
    overflow: hidden;
  }

  .voice-waveform i {
    width: 2px;
    height: calc(100% * var(--voice-sample));
    min-height: 3px;
    flex: 1 1 auto;
    border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 72%, var(--text-muted));
  }

  .voice-message :global(audio) {
    width: 100%;
    height: 34px;
  }
</style>
