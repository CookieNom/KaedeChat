<script lang="ts">
  import type { Attachment } from '$lib/chat/types';
  import VoiceMessageFrame from './VoiceMessageFrame.svelte';
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

<VoiceMessageFrame {duration} waveform={manifest.waveform} encrypted>
  {#if objectUrl}
    <audio src={objectUrl} controls preload="metadata" aria-label={`Play ${manifest.filename}`}>
      <track kind="captions" />
    </audio>
  {:else if unavailable}
    <button type="button" onclick={() => void load()}>Try decrypting again</button>
  {:else}
    <small role="status">Decrypting audio…</small>
  {/if}
</VoiceMessageFrame>

<style>
  button {
    justify-self: start;
  }
</style>
