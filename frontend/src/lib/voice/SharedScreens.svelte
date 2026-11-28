<script lang="ts">
  import { onDestroy, type Snippet } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import { isNativeDesktop } from '$lib/platform/native';
  import { t } from '$lib/ui/locale';
  import { attachVideo, type VoiceTile } from './session';
  import { showScreenFullscreen } from './screen-fullscreen';
  import ListeningVolumeMenu from './ListeningVolumeMenu.svelte';

  let {
    panel,
    screens,
    cameras,
    cameraTile,
    listeningVolume,
    setVolume,
    onError
  }: {
    panel: HTMLElement;
    screens: VoiceTile[];
    cameras: VoiceTile[];
    cameraTile: Snippet<[VoiceTile]>;
    listeningVolume: (identity: string, stream?: boolean) => number;
    setVolume: (identity: string, stream: boolean, value: number) => Promise<void>;
    onError: (message: string) => void;
  } = $props();

  let focused = $state('');
  let fullscreen = $state('');
  let fullscreenSession: AbortController | undefined;
  const fullscreenAvailable =
    isNativeDesktop() || (typeof document !== 'undefined' && document.fullscreenEnabled);
  const visible = $derived(
    fullscreen ? screens.filter((tile) => tile.key === fullscreen) : screens
  );

  $effect(() => {
    if (focused && (screens.length < 2 || !screens.some((tile) => tile.key === focused)))
      focused = '';
    if (fullscreen && !screens.some((tile) => tile.key === fullscreen)) fullscreenSession?.abort();
  });
  onDestroy(() => fullscreenSession?.abort());

  async function toggleFullscreen(key: string) {
    if (fullscreenSession) {
      fullscreenSession.abort();
      return;
    }
    fullscreenSession = new AbortController();
    fullscreen = key;
    try {
      await showScreenFullscreen(panel, fullscreenSession.signal);
    } catch {
      onError($t('voice_screen_fullscreen_error'));
    } finally {
      fullscreen = '';
      fullscreenSession = undefined;
    }
  }
</script>

<div class="screen-stage">
  <div
    class="screen-grid"
    class:multiple={visible.length > 1}
    class:focused={Boolean(focused && !fullscreen)}
    style:--alternatives={Math.max(1, visible.length - 1)}
  >
    {#each visible as tile (tile.key)}
      {@const title = tile.local
        ? $t('voice_screen_local')
        : $t('voice_screen_remote', { name: tile.name })}
      {@const focusLabel = $t('voice_screen_focus', { screen: title })}
      {@const thumbnail = Boolean(focused && !fullscreen && focused !== tile.key)}
      {@const selected = focused === tile.key}
      {@const fullscreenLabel = fullscreen
        ? $t('voice_screen_exit_fullscreen')
        : $t('voice_screen_fullscreen')}
      <article class="screen-tile" class:thumbnail class:selected aria-label={title}>
        <div class="screen-image" use:attachVideo={tile}></div>
        {#if !tile.nativeFrame && !tile.track}
          <div class="screen-waiting" role="status">
            <Icon name="screen" size={24} />
            <span>{$t('voice_screen_waiting')}</span>
          </div>
        {/if}
        {#if thumbnail}
          <button
            class="select-screen"
            aria-label={focusLabel}
            title={focusLabel}
            onclick={() => (focused = tile.key)}
          ></button>
        {/if}
        <div class="screen-caption share-overlay">
          <span class="screen-name"><Icon name="screen" size={15} /><span>{title}</span></span>
          {#if !thumbnail}
            <div class="screen-tools">
              {#if !tile.local}
                <ListeningVolumeMenu
                  name={tile.name}
                  voice={listeningVolume(tile.identity)}
                  stream={listeningVolume(tile.identity, true)}
                  hasStream
                  onChange={(stream, value) => setVolume(tile.identity, stream, value)}
                />
              {/if}
              {#if visible.length > 1}
                <button
                  aria-label={selected ? $t('voice_screen_show_all') : focusLabel}
                  title={selected ? $t('voice_screen_show_all') : focusLabel}
                  onclick={() => (focused = selected ? '' : tile.key)}
                  ><Icon name={selected ? 'grid' : 'focus'} size={19} /></button
                >
              {/if}
              {#if fullscreenAvailable}
                <button
                  aria-label={fullscreenLabel}
                  title={fullscreenLabel}
                  onclick={() => void toggleFullscreen(tile.key)}
                  ><Icon name={fullscreen ? 'exit-fullscreen' : 'fullscreen'} size={19} /></button
                >
              {/if}
            </div>
          {/if}
        </div>
      </article>
    {/each}
  </div>
  {#if cameras.length && !fullscreen}
    <div class="camera-strip">
      {#each cameras as tile (tile.key)}
        {@render cameraTile(tile)}
      {/each}
    </div>
  {/if}
</div>

<style>
  .screen-stage {
    display: flex;
    flex-direction: column;
    gap: 3px;
    width: 100%;
    height: 100%;
    min-height: 0;
    min-width: 0;
    background: #18191d;
    container: share-stage / size;
  }
  .screen-grid {
    display: grid;
    flex: 1;
    grid-auto-rows: minmax(0, 1fr);
    grid-template-columns: minmax(0, 1fr);
    min-width: 0;
    min-height: 0;
    gap: 3px;
  }
  .screen-grid.multiple {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .screen-grid.focused {
    grid-template-columns: minmax(0, 1fr) clamp(7rem, 22%, 18rem);
  }
  .focused .selected {
    grid-column: 1;
    grid-row: 1 / span var(--alternatives);
  }
  .thumbnail {
    grid-column: 2;
  }
  .screen-tile {
    position: relative;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    background: #08090b;
    container-type: size;
  }
  .screen-image,
  .screen-image :global(video),
  .screen-image :global(canvas) {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .screen-waiting {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    color: #ddd;
    font-size: 0.85rem;
  }
  .select-screen {
    position: absolute;
    inset: 0;
    width: 100%;
    border: 0;
    background: transparent;
    cursor: pointer;
  }
  .screen-caption {
    position: absolute;
    inset: auto 0.7rem 4.4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
    pointer-events: none;
  }
  .screen-name,
  .screen-tools {
    display: inline-flex;
    align-items: center;
    border-radius: 9px;
    color: #fff;
    background: rgb(18 19 22 / 92%);
  }
  .screen-name {
    min-width: 0;
    gap: 0.4rem;
    padding: 0.38rem 0.55rem;
    font-size: 0.78rem;
    font-weight: 650;
  }
  .screen-name :global(svg) {
    flex: none;
  }
  .screen-name span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .screen-tools {
    flex: none;
    padding: 2px;
    pointer-events: auto;
  }
  .screen-tools button,
  .screen-tools :global(.audio-menu > button) {
    display: grid;
    place-items: center;
    width: 2.45rem;
    height: 2.45rem;
    min-width: 0;
    border: 0;
    border-radius: 7px;
    padding: 0;
    color: white;
    background: transparent;
    cursor: pointer;
  }
  .screen-tools :global(.audio-menu) {
    position: static;
  }
  .screen-tools :global(.audio-menu > button svg + svg) {
    display: none;
  }
  .screen-tools button:hover,
  .screen-tools :global(.audio-menu > button:hover) {
    background: #383a41;
  }
  .screen-tools button:focus-visible,
  .select-screen:focus-visible {
    outline: 2px solid white;
    outline-offset: -3px;
  }
  .camera-strip {
    display: flex;
    flex: 0 1 23%;
    min-height: 0;
    max-height: 7rem;
    justify-content: safe center;
    gap: 3px;
    overflow-x: auto;
    background: #08090b;
  }
  .camera-strip :global(.video-tile) {
    flex: 0 0 auto;
    min-height: 0;
    aspect-ratio: 16 / 9;
    border: 0;
    border-radius: 0;
  }
  .thumbnail .screen-caption,
  .screen-grid:has(+ .camera-strip) .screen-caption {
    bottom: 0.5rem;
  }
  @container share-stage (max-width: 38rem) and (max-aspect-ratio: 7/4) {
    .screen-grid.multiple {
      grid-template-columns: minmax(0, 1fr);
    }
    .screen-grid.focused {
      grid-template-columns: repeat(var(--alternatives), minmax(0, 1fr));
      grid-template-rows: minmax(0, 1fr) 25%;
    }
    .focused .selected {
      grid-column: 1 / -1;
      grid-row: 1;
    }
    .thumbnail {
      grid-column: auto;
      grid-row: 2;
    }
    .multiple .screen-tile:not(:last-child) .screen-caption {
      bottom: 0.45rem;
    }
  }
  @container (max-height: 12rem) {
    .screen-caption {
      inset-inline: 0.4rem;
      bottom: 0.4rem;
    }
  }
  @container (max-width: 18rem) {
    .screen-caption {
      flex-wrap: wrap;
    }
    .screen-name {
      font-size: 0.72rem;
    }
  }
</style>
