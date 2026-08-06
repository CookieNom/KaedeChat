<script lang="ts">
  import { onDestroy } from 'svelte';

  let { file }: { file: File } = $props();
  let objectUrl = $state('');
  let previous: File | null = null;

  $effect(() => {
    if (file === previous) return;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    previous = file;
    objectUrl = URL.createObjectURL(file);
  });

  onDestroy(() => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  });
</script>

{#if file.type.startsWith('image/')}
  <img src={objectUrl} alt={`Preview of ${file.name}`} />
{:else if file.type.startsWith('video/')}
  <video src={objectUrl} muted aria-label={`Preview of ${file.name}`}></video>
{:else}
  <span class="file-glyph" aria-hidden="true"
    >{file.name.split('.').pop()?.slice(0, 4) ?? 'FILE'}</span
  >
{/if}

<style>
  img,
  video {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .file-glyph {
    display: grid;
    width: 100%;
    height: 100%;
    place-items: center;
    color: var(--text-muted);
    font-family: var(--font-mono);
    font-size: 0.68rem;
    font-weight: 800;
    text-transform: uppercase;
  }
</style>
