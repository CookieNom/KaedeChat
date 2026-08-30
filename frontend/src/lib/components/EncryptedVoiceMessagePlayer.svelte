<script lang="ts">
  import type { Attachment } from '$lib/chat/types';
  import { voiceDurationLabel, voiceWaveformSamples } from '$lib/chat/voice-messages';
  import { decryptEncryptedAttachment, type EncryptedFileManifest } from '$lib/e2ee/media';
  import { onDestroy } from 'svelte';

  let {
    manifest,
    attachment,
    onError
  }: {
    manifest: EncryptedFileManifest;
    attachment: Attachment | null;
    onError?: (error: unknown) => void;
  } = $props();

  let objectUrl = $state<string | null>(null);
  let unavailable = $state(false);
  let active = true;
  let generation = 0;
  let loadedKey = $state('');
  const mediaKey = $derived(
    JSON.stringify([
      manifest.file_id,
      manifest.key,
      manifest.ciphertext_sha256,
      attachment?.history_media_url ?? null,
      attachment?.private_media_url ?? null
    ])
  );
  const duration = $derived(
    manifest.duration_millis === undefined ? null : manifest.duration_millis / 1_000
  );
  const samples = $derived(voiceWaveformSamples(manifest.waveform));

  function revokeObjectUrl(): void {
    if (!objectUrl) return;
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }

  async function load(
    targetManifest = manifest,
    targetAttachment = attachment,
    targetKey = mediaKey
  ) {
    const attempt = ++generation;
    unavailable = false;
    try {
      const plaintext = await decryptEncryptedAttachment(
        targetManifest,
        targetAttachment?.history_media_url,
        targetAttachment?.private_media_url
      );
      if (!active || attempt !== generation || targetKey !== loadedKey) return;
      const nextObjectUrl = URL.createObjectURL(plaintext);
      revokeObjectUrl();
      objectUrl = nextObjectUrl;
    } catch (caught) {
      if (!active || attempt !== generation || targetKey !== loadedKey) return;
      unavailable = true;
      onError?.(caught);
    }
  }

  $effect(() => {
    const targetKey = mediaKey;
    if (targetKey === loadedKey) return;
    loadedKey = targetKey;
    generation += 1;
    unavailable = false;
    revokeObjectUrl();
    void load(manifest, attachment, targetKey);
  });

  onDestroy(() => {
    active = false;
    generation += 1;
    revokeObjectUrl();
  });
</script>

<div class="voice-message" aria-label={`Encrypted voice message · ${voiceDurationLabel(duration)}`}>
  <div class="voice-heading">
    <span aria-hidden="true">🔒🎙️</span>
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
  {#if objectUrl}
    <audio src={objectUrl} controls preload="metadata" aria-label={`Play ${manifest.filename}`}>
      <track kind="captions" />
    </audio>
  {:else if unavailable}
    <button type="button" onclick={() => void load()}>Try decrypting again</button>
  {:else}
    <small role="status">Decrypting audio…</small>
  {/if}
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

  audio {
    width: 100%;
    height: 34px;
  }

  button {
    justify-self: start;
  }
</style>
