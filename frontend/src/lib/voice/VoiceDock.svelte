<script lang="ts">
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import Icon from '$lib/components/Icon.svelte';
  import { Permission } from '$lib/generated/permissions';
  import { onDestroy, onMount } from 'svelte';
  import { isNativeDesktop } from '$lib/platform/native';

  import { attachVideo, VoiceSession, type VoiceToken } from './session';

  let {
    channelRef,
    callRef,
    permissions = null
  }: { channelRef?: string; callRef?: string; permissions?: string | null } = $props();
  const voice = new VoiceSession();
  let revision = $state(0);
  let error = $state('');
  let audioHost = $state<HTMLElement | null>(null);
  let detachAudio: (() => void) | null = null;
  let mounted = false;
  let connectionGeneration = 0;
  let joinController: AbortController | null = null;
  const permissionBits = $derived.by(() => {
    if (callRef || permissions === null) return null;
    try {
      return BigInt(permissions);
    } catch {
      return 0n;
    }
  });
  const canConnect = $derived(
    permissionBits === null ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.CONNECT))
  );
  const permittedToSpeak = $derived(
    permissionBits === null ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.SPEAK))
  );
  const permittedToStream = $derived(
    permissionBits === null ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.STREAM))
  );
  const voiceCapabilitySummary = $derived(
    !canConnect
      ? 'You do not have permission to connect'
      : !permittedToSpeak && !permittedToStream
        ? 'You may listen, but cannot speak, use video, or share your screen'
        : !permittedToSpeak
          ? 'You may listen and share video, but cannot speak'
          : !permittedToStream
            ? 'You may speak, but cannot use video or share your screen'
            : 'Join to talk, share video, or present your screen'
  );
  const view = $derived.by(() => {
    // VoiceSession deliberately owns the LiveKit lifecycle outside Svelte's
    // proxy system. Reading the revision here makes its event-driven state
    // changes visible to the template.
    void revision;
    return {
      connected: voice.connected,
      connecting: voice.connecting,
      microphone: voice.microphone,
      camera: voice.camera,
      screen: voice.screen,
      canSpeak: voice.canSpeak,
      canStream: voice.canStream,
      participants: voice.participants(),
      tiles: voice.tiles()
    };
  });

  const changed = () => {
    revision += 1;
    error = voice.error;
  };

  const moved = (event: Event) => {
    const grant = (event as CustomEvent<VoiceToken>).detail;
    if ((grant.move_session_id ?? null) !== voice.moveSessionId) return;
    const generation = ++connectionGeneration;
    joinController?.abort();
    joinController = null;
    void (async () => {
      try {
        await voice.disconnect();
        if (!mounted || generation !== connectionGeneration) return;
        if (isNativeDesktop()) {
          const reference = callRef ?? channelRef;
          if (reference) await voice.connectNative(reference, Boolean(callRef));
          return;
        }
        await voice.connect(grant);
        if (!mounted || generation !== connectionGeneration) await voice.disconnect();
      } catch (caught) {
        if (mounted && generation === connectionGeneration) {
          error = userErrorMessage(caught, 'Could not move voice rooms. Try joining again.');
        }
      }
    })();
  };

  onMount(() => {
    mounted = true;
    voice.addEventListener('change', changed);
    window.addEventListener('kaede:voice-token', moved);
    if (audioHost) detachAudio = voice.attachAudio(audioHost);
  });

  onDestroy(() => {
    mounted = false;
    connectionGeneration += 1;
    joinController?.abort();
    joinController = null;
    voice.removeEventListener('change', changed);
    window.removeEventListener('kaede:voice-token', moved);
    detachAudio?.();
    void voice.disconnect();
  });

  async function join() {
    if (!mounted) return;
    if (!canConnect) {
      error = 'You do not have permission to join this voice channel.';
      return;
    }
    const generation = ++connectionGeneration;
    joinController?.abort();
    const controller = new AbortController();
    joinController = controller;
    error = '';
    try {
      if (isNativeDesktop()) {
        const reference = callRef ?? channelRef;
        if (!reference) throw new Error('Voice channel is unavailable.');
        await voice.connectNative(reference, Boolean(callRef));
        return;
      }
      const path = callRef
        ? `/calls/${encodeURIComponent(callRef)}/voice/token`
        : `/channels/${encodeURIComponent(channelRef ?? '')}/voice/token`;
      const grant = await api<VoiceToken>(path, {
        method: 'POST',
        signal: controller.signal
      });
      if (!mounted || generation !== connectionGeneration || controller.signal.aborted) return;
      await voice.connect(grant);
      if (!mounted || generation !== connectionGeneration || controller.signal.aborted) {
        await voice.disconnect();
        return;
      }
      if (audioHost) {
        detachAudio?.();
        detachAudio = voice.attachAudio(audioHost);
      }
    } catch (caught) {
      if (mounted && generation === connectionGeneration && !controller.signal.aborted) {
        error =
          caught instanceof ApiError
            ? caught.code === 'MISSING_PERMISSIONS'
              ? 'You do not have permission to join this voice channel.'
              : caught.message
            : voice.error ||
              userErrorMessage(
                caught,
                'Could not join voice. Check your network and microphone permission, then try again.'
              );
      }
    } finally {
      if (joinController === controller) joinController = null;
    }
  }

  async function safely(action: () => Promise<void>) {
    error = '';
    try {
      await action();
    } catch (caught) {
      if (mounted) error = userErrorMessage(caught, 'Voice control failed. Try again.');
    }
  }
</script>

<section class="voice-panel" aria-label="Voice channel">
  <header class="voice-heading">
    <div class="voice-status">
      <span class:connected={view.connected} class="status-dot" aria-hidden="true"></span>
      <div>
        <strong>{view.connected ? 'Voice connected' : 'Voice channel'}</strong>
        <span>
          {view.connected
            ? `${view.participants.length} ${view.participants.length === 1 ? 'participant' : 'participants'}`
            : voiceCapabilitySummary}
        </span>
      </div>
    </div>
    {#if !view.connected}
      <button
        class="primary"
        disabled={view.connecting || !canConnect}
        title={!canConnect ? 'You do not have permission to join this voice channel.' : undefined}
        onclick={join}
      >
        {view.connecting ? 'Connecting…' : 'Join voice'}
      </button>
    {/if}
  </header>

  <div class="audio-host" bind:this={audioHost}></div>

  <main class="voice-stage">
    {#if error}<p class="voice-error" role="alert">{error}</p>{/if}
    {#if view.connected}
      {#if view.tiles.length > 0}
        <div
          class="video-grid"
          class:has-screen={view.tiles.some((tile) => tile.source === 'screen_share')}
        >
          {#each view.tiles as tile (tile.key)}
            <article class:screen-tile={tile.source === 'screen_share'} class="video-tile">
              <div class="video-host" use:attachVideo={tile}></div>
              <span>{tile.name}{tile.local ? ' (you)' : ''}</span>
            </article>
          {/each}
        </div>
      {:else}
        <div class="participant-grid">
          {#each view.participants as participant (participant.key)}
            <article class:speaking={participant.speaking} class="participant-card">
              <div class="participant-avatar" aria-hidden="true">
                {participant.name.slice(0, 1).toUpperCase()}
              </div>
              <div class="participant-name">
                <strong>{participant.name}</strong>
                {#if participant.local}<span>You</span>{/if}
              </div>
              <span
                class:muted={!participant.microphone}
                class="participant-mic"
                title={participant.microphone ? 'Microphone on' : 'Muted'}
              >
                <Icon name={participant.microphone ? 'microphone' : 'microphone-off'} size={17} />
              </span>
            </article>
          {/each}
        </div>
      {/if}
    {:else if !canConnect}
      <div class="join-prompt permission-prompt">
        <span><Icon name="lock" size={26} /></span>
        <strong>You cannot join this voice channel</strong>
        <p>Your roles do not include the Connect permission for this channel.</p>
      </div>
    {:else}
      <div class="join-prompt">
        <span><Icon name="volume" size={28} /></span>
        <strong>Ready when you are</strong>
        <p>Join the room to talk with everyone already here.</p>
      </div>
    {/if}
  </main>

  {#if view.connected}
    {#if !view.canSpeak || !view.canStream}
      <div class="voice-permission-notice" role="status">
        {#if !view.canSpeak && !view.canStream}
          You can listen, but your roles do not allow speaking, camera, or screen sharing here.
        {:else if !view.canSpeak}
          You can listen and share video, but your roles do not allow speaking here.
        {:else}
          You can speak, but your roles do not allow camera or screen sharing here.
        {/if}
      </div>
    {/if}
    <footer class="voice-dock" aria-label="Voice controls">
      <button
        class:off={!view.microphone}
        class="control-button"
        disabled={!view.canSpeak}
        aria-pressed={view.microphone}
        aria-label={view.microphone ? 'Mute microphone' : 'Unmute microphone'}
        title={!view.canSpeak
          ? 'You do not have permission to speak in this channel.'
          : view.microphone
            ? 'Mute'
            : 'Unmute'}
        onclick={() => safely(() => voice.toggleMicrophone())}
      >
        <Icon name={view.microphone ? 'microphone' : 'microphone-off'} size={20} />
      </button>
      <button
        class:active={view.camera}
        class="control-button"
        disabled={!view.canStream}
        aria-pressed={view.camera}
        aria-label={view.camera ? 'Turn camera off' : 'Turn camera on'}
        title={!view.canStream
          ? 'You do not have permission to use video in this channel.'
          : view.camera
            ? 'Camera off'
            : 'Camera on'}
        onclick={() => safely(() => voice.toggleCamera())}
      >
        <Icon name={view.camera ? 'video' : 'video-off'} size={21} />
      </button>
      <button
        class:active={view.screen}
        class="control-button"
        disabled={!view.canStream}
        aria-pressed={view.screen}
        aria-label={view.screen ? 'Stop sharing screen' : 'Share screen'}
        title={!view.canStream
          ? 'You do not have permission to share your screen in this channel.'
          : view.screen
            ? 'Stop sharing'
            : 'Share screen'}
        onclick={() => safely(() => voice.toggleScreen())}
      >
        <Icon name="screen" size={21} />
      </button>
      <span class="control-divider" aria-hidden="true"></span>
      <button
        class="control-button danger"
        aria-label="Leave voice"
        title="Leave voice"
        onclick={() => safely(() => voice.disconnect())}
      >
        <Icon name="phone-off" size={21} />
      </button>
    </footer>
  {/if}
</section>

<style>
  .voice-panel {
    display: grid;
    width: 100%;
    height: 100%;
    min-height: 0;
    grid-template-rows: auto minmax(0, 1fr) auto;
    gap: 0;
    overflow: hidden;
    background: var(--paper);
  }

  .voice-permission-notice {
    border-top: 1px solid var(--line-soft);
    padding: 0.65rem 1rem;
    color: var(--text-muted);
    background: color-mix(in srgb, var(--surface-subtle) 82%, transparent);
    font-size: 0.78rem;
    text-align: center;
  }

  .permission-prompt span {
    color: var(--text-muted);
  }

  .voice-heading {
    display: flex;
    min-width: 0;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line);
    padding: 0.85rem clamp(1rem, 2.5vw, 1.6rem);
    background: color-mix(in srgb, var(--paper-raised) 42%, var(--paper));
  }

  .voice-status {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.7rem;
  }

  .voice-status > div {
    display: grid;
    min-width: 0;
    gap: 0.12rem;
  }

  .voice-status strong,
  .voice-status span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .voice-status strong {
    font-size: 0.88rem;
  }

  .voice-status span {
    color: var(--ink-soft);
    font-size: 0.72rem;
  }

  .status-dot {
    width: 0.62rem;
    height: 0.62rem;
    flex: 0 0 auto;
    border-radius: 999px;
    background: var(--text-muted);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--text-muted) 12%, transparent);
  }

  .status-dot.connected {
    background: var(--pine);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--pine) 14%, transparent);
  }

  .primary {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.52rem 0.78rem;
    background: var(--maple);
    color: var(--on-accent);
    border-color: var(--maple);
    font-size: 0.74rem;
    font-weight: 720;
    cursor: pointer;
    transition: filter 140ms ease;
  }

  .primary:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .voice-error {
    position: absolute;
    top: 1rem;
    left: 50%;
    z-index: 2;
    width: min(32rem, calc(100% - 2rem));
    transform: translateX(-50%);
    margin: 0;
    border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--line));
    border-radius: 11px;
    padding: 0.65rem 0.8rem;
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 12%, var(--paper));
    font-size: 0.8rem;
  }

  .audio-host {
    display: none;
  }

  .voice-stage {
    position: relative;
    display: grid;
    min-height: 0;
    place-items: center;
    overflow: auto;
    padding: clamp(1rem, 3vw, 2.5rem);
    background:
      radial-gradient(
        circle at 50% 42%,
        color-mix(in srgb, var(--maple) 5%, transparent),
        transparent 32rem
      ),
      var(--paper);
  }

  .video-grid {
    display: grid;
    width: min(100%, 76rem);
    min-height: 0;
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
    gap: 0.8rem;
  }

  .video-grid.has-screen {
    grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr);
  }

  .video-tile {
    position: relative;
    min-height: 210px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: #121211;
    box-shadow: 0 12px 30px rgb(0 0 0 / 14%);
  }

  .video-tile.screen-tile {
    grid-row: span 2;
    min-height: min(420px, 48vh);
  }

  .video-host {
    width: 100%;
    height: 100%;
  }

  .video-host :global(video) {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .video-host :global(canvas) {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .screen-tile .video-host :global(video) {
    object-fit: contain;
  }

  .video-tile > span {
    position: absolute;
    left: 0.7rem;
    bottom: 0.7rem;
    padding: 0.25rem 0.5rem;
    border-radius: 999px;
    background: rgb(0 0 0 / 72%);
    color: white;
    font-size: 0.72rem;
    font-weight: 650;
    backdrop-filter: blur(8px);
  }

  .participant-grid {
    display: grid;
    width: min(100%, 64rem);
    grid-template-columns: repeat(auto-fit, minmax(min(230px, 100%), 280px));
    justify-content: center;
    gap: 0.8rem;
  }

  .participant-card {
    position: relative;
    display: grid;
    aspect-ratio: 4 / 3;
    place-items: center;
    align-content: center;
    gap: 0.85rem;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.25rem;
    background: color-mix(in srgb, var(--paper-raised) 72%, var(--paper));
    box-shadow: 0 8px 22px rgb(0 0 0 / 10%);
    transition:
      border-color 120ms ease,
      box-shadow 120ms ease;
  }

  .participant-card.speaking {
    border-color: var(--pine);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--pine) 50%, transparent);
  }

  .participant-avatar {
    display: grid;
    width: clamp(4rem, 7vw, 5.25rem);
    height: clamp(4rem, 7vw, 5.25rem);
    place-items: center;
    border-radius: 50%;
    color: var(--on-accent);
    background: linear-gradient(
      145deg,
      var(--maple),
      color-mix(in srgb, var(--maple) 62%, #5f426f)
    );
    font-size: 1.65rem;
    font-weight: 800;
    box-shadow: inset 0 0 0 1px rgb(255 255 255 / 13%);
  }

  .participant-name {
    display: flex;
    max-width: 100%;
    align-items: center;
    gap: 0.4rem;
  }

  .participant-name strong {
    overflow: hidden;
    font-size: 0.86rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .participant-name span {
    border-radius: 999px;
    padding: 0.15rem 0.38rem;
    color: var(--ink-soft);
    background: var(--paper);
    font-size: 0.6rem;
    font-weight: 700;
  }

  .participant-mic {
    position: absolute;
    right: 0.7rem;
    bottom: 0.7rem;
    display: grid;
    width: 1.85rem;
    height: 1.85rem;
    place-items: center;
    border: 1px solid var(--line);
    border-radius: 50%;
    color: var(--pine);
    background: var(--paper);
  }

  .participant-mic.muted {
    color: var(--danger);
  }

  .join-prompt {
    display: grid;
    max-width: 26rem;
    place-items: center;
    gap: 0.55rem;
    text-align: center;
  }

  .join-prompt > span {
    display: grid;
    width: 4rem;
    height: 4rem;
    place-items: center;
    margin-bottom: 0.25rem;
    border-radius: 50%;
    color: var(--maple);
    background: color-mix(in srgb, var(--maple) 12%, var(--paper-raised));
  }

  .join-prompt strong {
    font-size: 1rem;
  }

  .join-prompt p {
    margin: 0;
    color: var(--ink-soft);
    font-size: 0.78rem;
  }

  .voice-dock {
    display: flex;
    align-items: center;
    justify-self: center;
    gap: 0.4rem;
    width: fit-content;
    max-width: 100%;
    margin: 0 auto 1.15rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.4rem;
    background: color-mix(in srgb, var(--paper-raised) 94%, transparent);
    box-shadow: 0 10px 26px rgb(0 0 0 / 16%);
    backdrop-filter: blur(14px);
  }

  .control-button {
    display: grid;
    width: 2.85rem;
    height: 2.85rem;
    flex: 0 0 auto;
    place-items: center;
    border: 0;
    border-radius: 50%;
    padding: 0;
    color: var(--ink);
    background: color-mix(in srgb, var(--ink) 8%, transparent);
    cursor: pointer;
    transition:
      background-color 120ms ease,
      color 120ms ease,
      transform 120ms ease;
  }

  .control-button:hover:not(:disabled) {
    background: color-mix(in srgb, var(--ink) 14%, transparent);
    transform: translateY(-1px);
  }

  .control-button.active {
    color: var(--on-accent);
    background: var(--maple);
  }

  .control-button.off,
  .control-button.danger {
    color: white;
    background: var(--danger);
  }

  .control-button.danger:hover:not(:disabled),
  .control-button.off:hover:not(:disabled) {
    background: color-mix(in srgb, var(--danger) 82%, black);
  }

  .control-divider {
    width: 1px;
    height: 1.7rem;
    margin-inline: 0.12rem;
    background: var(--line);
  }

  @media (max-width: 720px) {
    .voice-heading {
      padding-inline: 0.85rem;
    }

    .voice-status span {
      max-width: 44vw;
    }

    .voice-stage {
      padding: 0.85rem;
    }

    .video-grid.has-screen {
      grid-template-columns: 1fr;
    }

    .video-tile.screen-tile {
      min-height: 260px;
    }

    .participant-grid {
      grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr));
    }

    .voice-dock {
      margin-bottom: 0.75rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .primary,
    .participant-card,
    .control-button {
      transition: none;
    }
  }
</style>
