<script lang="ts">
  import ListeningVolumeMenu from '$lib/voice/ListeningVolumeMenu.svelte';
  import { onMount } from 'svelte';
  import { isNativeDesktop, nativeInvoke, type NativeVoiceStatus } from '$lib/platform/native';
  import { t } from '$lib/ui/locale';
  import {
    attachVideo,
    decodeNativeVideoFrame,
    NativeVideoPresentation,
    type VoiceTile
  } from '$lib/voice/session';

  let tiles = $state.raw<VoiceTile[]>([]);
  let volumes = $state<Record<string, [number, number]>>({});
  onMount(() => {
    if (!isNativeDesktop()) return;
    let stopped = false;
    const presentation = new NativeVideoPresentation();
    const updateTiles = () => {
      tiles = presentation.tiles().filter((tile) => !tile.local);
    };
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const status = await nativeInvoke<NativeVoiceStatus>('native_voice_status');
        if (stopped) return;
        presentation.setStatus(status);
        updateTiles();
        volumes = status.listening_volumes ?? {};
      } catch {
        // Keep the current view until the next successful poll.
      } finally {
        polling = false;
      }
    };
    void poll();
    const volumePoll = setInterval(() => void poll(), 500);
    void (async () => {
      while (!stopped) {
        try {
          const response = await nativeInvoke<ArrayBuffer | Uint8Array>('native_voice_next_video');
          if (stopped) break;
          const bytes = response instanceof Uint8Array ? response : new Uint8Array(response);
          const frame = decodeNativeVideoFrame(bytes);
          if (!frame) {
            await new Promise((resolve) => setTimeout(resolve, 50));
            continue;
          }
          if (!frame.local) {
            presentation.applyFrame(frame);
            updateTiles();
          }
        } catch {
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
      }
    })();
    return () => {
      stopped = true;
      clearInterval(volumePoll);
    };
  });
</script>

<svelte:head><title>Kaede · Call video</title></svelte:head>
<main>
  {#each tiles as tile (tile.key)}
    <div class="tile">
      <div class="video" use:attachVideo={tile}></div>
      {#if !tile.nativeFrame}<p class="waiting">{$t('voice_screen_waiting')}</p>{/if}
      <span
        >{tile.source === 'screen_share'
          ? $t('voice_screen_remote', { name: tile.name })
          : tile.name}</span
      >
      <ListeningVolumeMenu
        name={tile.name}
        voice={volumes[tile.identity]?.[0] ?? 1}
        stream={volumes[tile.identity]?.[1] ?? 1}
        hasStream={tile.source === 'screen_share'}
        onChange={(stream, volume) =>
          nativeInvoke('native_voice_volume', { identity: tile.identity, stream, volume })}
      />
    </div>
  {:else}
    <p>
      {isNativeDesktop()
        ? 'Waiting for call video…'
        : 'Video pop-out is available in the desktop app.'}
    </p>
  {/each}
</main>

<style>
  main {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
    height: 100dvh;
    gap: 4px;
    background: #101014;
    color: white;
  }
  .tile {
    min-height: 0;
    min-width: 0;
    position: relative;
  }
  .video {
    width: 100%;
    height: 100%;
  }
  .video :global(canvas) {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .waiting {
    position: absolute;
    inset: 0;
    display: grid;
    place-content: center;
    pointer-events: none;
  }
  span {
    position: absolute;
    bottom: 8px;
    left: 8px;
    background: #0009;
    padding: 4px;
  }
  p {
    margin: auto;
  }
</style>
