<script lang="ts">
  import ListeningVolumeMenu from '$lib/voice/ListeningVolumeMenu.svelte';
  import { onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { isNativeDesktop, nativeInvoke, type NativeVoiceStatus } from '$lib/platform/native';
  import { attachVideo, decodeNativeVideoFrame, type VoiceTile } from '$lib/voice/session';

  const tiles = new SvelteMap<string, VoiceTile>();
  let volumes = $state<Record<string, [number, number]>>({});
  onMount(() => {
    if (!isNativeDesktop()) return;
    let stopped = false;
    const volumePoll = setInterval(() => {
      void nativeInvoke<NativeVoiceStatus>('native_voice_status')
        .then((status) => {
          if (!stopped) volumes = status.listening_volumes ?? {};
        })
        .catch(() => {});
    }, 500);
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
          const key = `${frame.participant}:${frame.source}`;
          if (frame.removed) tiles.delete(key);
          else
            tiles.set(key, {
              key,
              identity: frame.participant,
              name: frame.participant,
              source: frame.source,
              nativeFrame: frame.image,
              local: false
            });
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
  {#each [...tiles.values()] as tile (tile.key)}
    <div class="tile">
      <div class="video" use:attachVideo={tile}></div>
      <span>{tile.name}</span>
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
