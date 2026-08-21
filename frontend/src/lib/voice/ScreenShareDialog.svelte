<script lang="ts">
  import { tick } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import {
    isNativeDesktop,
    nativeInvoke,
    type NativeDevices,
    type NativePlatformInfo,
    type NativeScreenSource
  } from '$lib/platform/native';
  import {
    AUDIO_QUALITIES,
    DEFAULT_MEDIA_QUALITY,
    SCREEN_SHARE_PROFILES,
    loadMediaQuality,
    saveMediaQuality,
    screenShareProfile,
    type AudioQualityId,
    type MediaQualityPreferences,
    type ScreenShareProfileId
  } from './quality';

  let {
    open = $bindable(false),
    onShare
  }: {
    open?: boolean;
    onShare: (preferences: MediaQualityPreferences, sourceId: string | null) => Promise<void>;
  } = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let sharing = $state(false);
  let loadingSources = $state(false);
  let error = $state('');
  let sourceTab = $state<'application' | 'screen'>('application');
  let sources = $state<NativeScreenSource[]>([]);
  let thumbnails = $state<Record<string, string>>({});
  let selectedSourceId = $state<string | null>(null);
  let nativeOs = $state('');
  let preferences = $state<MediaQualityPreferences>({ ...DEFAULT_MEDIA_QUALITY });
  let loadGeneration = 0;

  const native = isNativeDesktop();
  const visibleSources = $derived(
    sources.filter((source) =>
      sourceTab === 'application'
        ? (source.kind ?? (source.id.startsWith('window:') ? 'application' : 'screen')) ===
          'application'
        : (source.kind ?? (source.id.startsWith('window:') ? 'application' : 'screen')) === 'screen'
    )
  );
  const selectedProfile = $derived(screenShareProfile(preferences.screenProfile));
  const securePicker = $derived(native && sources.length === 0);

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      preferences = { ...loadMediaQuality(), ...(native ? { shareAudio: false } : {}) };
      error = '';
      selectedSourceId = null;
      sourceTab = 'application';
      dialog.showModal();
      void tick().then(() => dialog?.querySelector<HTMLElement>('[data-initial-focus]')?.focus());
      if (native) void loadNativeSources();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  async function loadNativeSources() {
    const generation = ++loadGeneration;
    loadingSources = true;
    error = '';
    try {
      const [devices, platform] = await Promise.all([
        nativeInvoke<NativeDevices>('native_audio_devices'),
        nativeInvoke<NativePlatformInfo>('native_platform_info')
      ]);
      if (generation !== loadGeneration || !open) return;
      sources = devices.screens.slice(0, 48);
      nativeOs = platform.os;
      const applications = sources.filter((source) => source.id.startsWith('window:'));
      const displays = sources.filter((source) => !source.id.startsWith('window:'));
      sourceTab = applications.length > 0 ? 'application' : 'screen';
      selectedSourceId = (applications[0] ?? displays[0])?.id ?? null;
      void loadThumbnails(generation, sources.slice(0, 24));
    } catch (caught) {
      if (generation !== loadGeneration || !open) return;
      error = caught instanceof Error ? caught.message : 'Could not list screens and windows.';
    } finally {
      if (generation === loadGeneration) loadingSources = false;
    }
  }

  async function loadThumbnails(generation: number, candidates: NativeScreenSource[]) {
    // A small worker pool prevents a many-window desktop from saturating the
    // capture backend or retaining full-size frames in the WebView.
    let index = 0;
    const worker = async () => {
      while (index < candidates.length && generation === loadGeneration && open) {
        const source = candidates[index++];
        try {
          const response = await nativeInvoke<ArrayBuffer | Uint8Array>('native_screen_thumbnail', {
            sourceId: source.id
          });
          const bytes = response instanceof Uint8Array ? response : new Uint8Array(response);
          const thumbnail = thumbnailDataUrl(bytes);
          if (thumbnail && generation === loadGeneration && open) {
            thumbnails = { ...thumbnails, [source.id]: thumbnail };
          }
        } catch {
          // A window may close or revoke capture while the chooser is open.
          // Keep its label card usable; sharing will perform a fresh check.
        }
      }
    };
    await Promise.all([worker(), worker(), worker()]);
  }

  function thumbnailDataUrl(bytes: Uint8Array): string | null {
    if (bytes.byteLength < 12 || new TextDecoder().decode(bytes.subarray(0, 4)) !== 'KST1') {
      return null;
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const width = view.getUint32(4, true);
    const height = view.getUint32(8, true);
    if (
      !width ||
      !height ||
      width > 512 ||
      height > 512 ||
      bytes.byteLength !== 12 + width * height * 4
    ) {
      return null;
    }
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) return null;
    const rgba = new Uint8ClampedArray(bytes.slice(12).buffer);
    context.putImageData(new ImageData(rgba, width, height), 0, 0);
    return canvas.toDataURL('image/webp', 0.78);
  }

  function close() {
    if (sharing) return;
    loadGeneration += 1;
    open = false;
  }

  function reset() {
    loadGeneration += 1;
    open = false;
    sharing = false;
    error = '';
    sources = [];
    thumbnails = {};
    selectedSourceId = null;
  }

  async function share() {
    if (sharing) return;
    if (native && !securePicker && !selectedSourceId) {
      error = 'Choose a window or display to share.';
      return;
    }
    sharing = true;
    error = '';
    saveMediaQuality(preferences);
    try {
      await onShare(preferences, selectedSourceId);
      open = false;
    } catch (caught) {
      error =
        caught instanceof Error
          ? caught.message
          : 'Screen sharing could not start. Check system privacy settings and try again.';
    } finally {
      sharing = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="share-dialog"
  aria-labelledby="screen-share-title"
  onclose={reset}
  oncancel={(event) => {
    event.preventDefault();
    close();
  }}
  onclick={(event) => {
    if (event.target === dialog) close();
  }}
>
  <section class="share-shell">
    <header class="share-heading">
      <div>
        <p>Go live</p>
        <h2 id="screen-share-title">Share your screen</h2>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label="Close"
        disabled={sharing}
        onclick={close}>×</button
      >
    </header>

    {#if native}
      <nav class="source-tabs" aria-label="Screen-share source type">
        <button
          type="button"
          data-initial-focus
          class:active={sourceTab === 'application'}
          aria-pressed={sourceTab === 'application'}
          onclick={() => (sourceTab = 'application')}
        >
          <Icon name="image" size={19} /> Applications
        </button>
        <button
          type="button"
          class:active={sourceTab === 'screen'}
          aria-pressed={sourceTab === 'screen'}
          onclick={() => (sourceTab = 'screen')}
        >
          <Icon name="screen" size={19} /> Entire screen
        </button>
      </nav>

      <div class="source-region" aria-live="polite">
        {#if loadingSources}
          <div class="source-message"><span class="spinner"></span>Finding shareable sources…</div>
        {:else if securePicker}
          <div class="secure-picker">
            <span><Icon name="shield" size={30} /></span>
            <h3>The system chooser will open next</h3>
            <p>
              {nativeOs === 'macos'
                ? 'macOS keeps available windows private until you use its secure sharing picker.'
                : 'This desktop session uses a privacy-preserving system picker, which is common on Wayland.'}
            </p>
          </div>
        {:else if visibleSources.length === 0}
          <div class="secure-picker">
            <span><Icon name="screen" size={30} /></span>
            <h3>No {sourceTab === 'application' ? 'applications' : 'displays'} available</h3>
            <p>Open a window or connect a display, then refresh the list.</p>
            <button class="secondary" type="button" onclick={() => void loadNativeSources()}
              >Refresh</button
            >
          </div>
        {:else}
          <div class="source-grid">
            {#each visibleSources as source (source.id)}
              <button
                type="button"
                class="source-card"
                class:selected={selectedSourceId === source.id}
                aria-pressed={selectedSourceId === source.id}
                onclick={() => (selectedSourceId = source.id)}
              >
                <span class="source-preview">
                  {#if thumbnails[source.id]}
                    <img src={thumbnails[source.id]} alt="" />
                  {:else}
                    <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={34} />
                  {/if}
                  {#if selectedSourceId === source.id}
                    <span class="selected-check"><Icon name="check" size={15} /></span>
                  {/if}
                </span>
                <strong>{source.label.replace(/^(Window|Display):\s*/, '')}</strong>
              </button>
            {/each}
          </div>
        {/if}
      </div>
    {:else}
      <div class="browser-picker" data-initial-focus tabindex="-1">
        <span><Icon name="shield" size={32} /></span>
        <div>
          <h3>Your browser protects the source list</h3>
          <p>
            After you press Share, your browser will ask you to choose a tab, window, or screen.
            Kaede cannot see those choices until you approve one.
          </p>
        </div>
      </div>
    {/if}

    <div class="quality-panel">
      <div class="quality-summary">
        <div>
          <p>Stream quality</p>
          <strong>{selectedProfile.label}</strong>
          <span>
            {selectedProfile.description} · {selectedProfile.height
              ? `${selectedProfile.height}p`
              : 'Source'} ·
            {selectedProfile.frameRate} FPS
          </span>
        </div>
        <span class="quality-badge"
          >{selectedProfile.height ? `${selectedProfile.height}p` : 'MAX'}</span
        >
      </div>

      <fieldset>
        <legend>Video</legend>
        <div class="option-grid video-options">
          {#each SCREEN_SHARE_PROFILES as profile (profile.id)}
            <label class:selected={preferences.screenProfile === profile.id}>
              <input
                type="radio"
                name="screen-quality"
                value={profile.id}
                checked={preferences.screenProfile === profile.id}
                onchange={() => (preferences.screenProfile = profile.id as ScreenShareProfileId)}
              />
              <strong>{profile.height ? `${profile.height}p` : 'Source'}</strong>
              <span>{profile.frameRate} FPS</span>
            </label>
          {/each}
        </div>
      </fieldset>

      <fieldset>
        <legend>Outgoing audio</legend>
        <div class="option-grid audio-options">
          {#each AUDIO_QUALITIES as quality (quality.id)}
            <label class:selected={preferences.audioQuality === quality.id}>
              <input
                type="radio"
                name="audio-quality"
                value={quality.id}
                checked={preferences.audioQuality === quality.id}
                onchange={() => (preferences.audioQuality = quality.id as AudioQualityId)}
              />
              <strong>{quality.label}</strong>
              <span>{quality.description}</span>
            </label>
          {/each}
        </div>
        <p class="bitrate-note">
          Bitrate is an upper target. Voice automatically uses less bandwidth during silence or
          network congestion.
        </p>
      </fieldset>

      <label class="audio-toggle">
        <span>
          <strong>Share computer audio</strong>
          <small>
            {native
              ? 'Desktop system-audio capture is not available in this build.'
              : 'Availability depends on the selected source and browser.'}
          </small>
        </span>
        <input type="checkbox" bind:checked={preferences.shareAudio} disabled={native} />
      </label>
    </div>

    {#if error}<p class="share-error" role="alert">{error}</p>{/if}

    <footer class="share-actions">
      <button class="secondary" type="button" disabled={sharing} onclick={close}>Cancel</button>
      <button
        class="primary"
        type="button"
        disabled={sharing || loadingSources}
        onclick={() => void share()}
      >
        <Icon name="screen" size={18} />
        {sharing ? 'Starting…' : native && !securePicker ? 'Share' : 'Choose source'}
      </button>
    </footer>
  </section>
</dialog>

<style>
  .share-dialog {
    width: min(1060px, calc(100vw - 28px));
    max-height: min(900px, calc(100dvh - 28px));
    padding: 0;
    overflow: hidden;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: 24px;
    background: var(--surface);
    box-shadow: var(--shadow-lg);
  }

  .share-dialog::backdrop {
    background: rgb(0 0 0 / 72%);
    backdrop-filter: blur(3px);
  }

  .share-shell {
    display: grid;
    max-height: min(900px, calc(100dvh - 28px));
    grid-template-rows: auto auto minmax(180px, 1fr) auto auto auto;
    overflow: hidden;
  }

  .share-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 20px 24px 16px;
  }

  .share-heading p,
  .share-heading h2,
  .quality-summary p,
  .quality-summary span,
  .secure-picker h3,
  .secure-picker p,
  .browser-picker h3,
  .browser-picker p,
  .bitrate-note,
  .share-error {
    margin: 0;
  }

  .share-heading p,
  .quality-summary p {
    color: var(--accent-text);
    font-size: 0.69rem;
    font-weight: 800;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .share-heading h2 {
    margin-top: 2px;
    font-family: var(--font-display);
    font-size: clamp(1.35rem, 3vw, 1.8rem);
  }

  .icon-button {
    width: 36px;
    height: 36px;
    padding: 0;
    color: var(--text-muted);
    border: 0;
    border-radius: 10px;
    background: transparent;
    font-size: 1.8rem;
    line-height: 1;
    cursor: pointer;
  }

  .icon-button:hover {
    background: var(--surface-hover);
  }

  .source-tabs {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 5px;
    margin: 0 24px;
    padding: 5px;
    border-radius: 15px;
    background: var(--rail);
  }

  .source-tabs button {
    display: flex;
    min-height: 50px;
    align-items: center;
    justify-content: center;
    gap: 9px;
    color: var(--rail-text);
    border: 0;
    border-radius: 11px;
    background: transparent;
    font-weight: 720;
    cursor: pointer;
    opacity: 0.68;
  }

  .source-tabs button.active {
    background: var(--rail-hover);
    opacity: 1;
  }

  .source-region {
    min-height: 180px;
    overflow: auto;
    padding: 20px 24px;
  }

  .source-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .source-card {
    display: grid;
    min-width: 0;
    gap: 8px;
    padding: 0;
    overflow: hidden;
    color: var(--text);
    text-align: left;
    border: 2px solid transparent;
    border-radius: 14px;
    background: transparent;
    cursor: pointer;
  }

  .source-card.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 16%, transparent);
  }

  .source-card > strong {
    padding: 0 10px 10px;
    overflow: hidden;
    font-size: 0.79rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-preview {
    position: relative;
    display: grid;
    min-height: 150px;
    place-items: center;
    overflow: hidden;
    color: var(--text-muted);
    border: 1px solid var(--line-soft);
    border-radius: 11px;
    background:
      linear-gradient(
        145deg,
        color-mix(in srgb, var(--surface-raised) 76%, transparent),
        transparent
      ),
      var(--surface-subtle);
  }

  .source-preview img {
    display: block;
    width: 100%;
    height: 100%;
    max-height: 240px;
    object-fit: contain;
    background: #111;
  }

  .selected-check {
    position: absolute;
    top: 9px;
    right: 9px;
    display: grid;
    width: 25px;
    height: 25px;
    place-items: center;
    color: var(--on-accent);
    border-radius: 999px;
    background: var(--accent);
  }

  .source-message,
  .secure-picker,
  .browser-picker {
    min-height: 180px;
  }

  .source-message {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    color: var(--text-muted);
  }

  .spinner {
    width: 18px;
    height: 18px;
    border: 2px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 800ms linear infinite;
  }

  .secure-picker,
  .browser-picker {
    display: grid;
    place-content: center;
    gap: 10px;
    padding: 26px;
    color: var(--text-muted);
    text-align: center;
    border: 1px dashed var(--line-strong);
    border-radius: 16px;
    background: var(--surface-subtle);
  }

  .secure-picker > span,
  .browser-picker > span {
    color: var(--accent-text);
  }

  .secure-picker h3,
  .browser-picker h3 {
    color: var(--text);
  }

  .browser-picker {
    grid-template-columns: auto minmax(0, 1fr);
    min-height: 150px;
    margin: 0 24px 20px;
    align-items: center;
    text-align: left;
  }

  .quality-panel {
    display: grid;
    gap: 15px;
    padding: 18px 24px;
    border-top: 1px solid var(--line-soft);
    background: color-mix(in srgb, var(--surface-subtle) 72%, var(--surface));
  }

  .quality-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  .quality-summary > div {
    display: grid;
    gap: 2px;
  }

  .quality-summary > div > span {
    color: var(--text-muted);
    font-size: 0.78rem;
  }

  .quality-badge {
    padding: 9px 12px;
    color: var(--accent-text);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--line));
    border-radius: 10px;
    background: var(--accent-soft);
    font-size: 0.75rem;
    font-weight: 850;
  }

  fieldset {
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  legend {
    margin-bottom: 7px;
    color: var(--text-soft);
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  .option-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 7px;
  }

  .option-grid label {
    display: grid;
    gap: 1px;
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface-raised);
    cursor: pointer;
  }

  .option-grid label.selected {
    color: var(--accent-text);
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .option-grid input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .option-grid strong {
    overflow: hidden;
    font-size: 0.78rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .option-grid span {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.67rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .bitrate-note {
    margin-top: 6px;
    color: var(--text-muted);
    font-size: 0.68rem;
  }

  .audio-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 11px 13px;
    border: 1px solid var(--line-soft);
    border-radius: 11px;
    background: var(--surface-raised);
  }

  .audio-toggle span {
    display: grid;
  }

  .audio-toggle small {
    color: var(--text-muted);
  }

  .audio-toggle input {
    width: 18px;
    height: 18px;
    accent-color: var(--accent);
  }

  .share-error {
    margin: 0 24px;
    padding: 10px 12px;
    color: var(--danger);
    border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--line));
    border-radius: 10px;
    background: var(--danger-soft);
    font-size: 0.78rem;
  }

  .share-actions {
    display: flex;
    justify-content: flex-end;
    gap: 9px;
    padding: 16px 24px 20px;
    border-top: 1px solid var(--line-soft);
    background: var(--surface);
  }

  .primary,
  .secondary {
    display: inline-flex;
    min-height: 40px;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 0 16px;
    border: 1px solid var(--line);
    border-radius: 10px;
    font-weight: 760;
    cursor: pointer;
  }

  .primary {
    color: var(--on-accent);
    border-color: var(--accent);
    background: var(--accent);
  }

  .secondary {
    background: var(--surface-raised);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 700px) {
    .share-dialog {
      width: min(100vw - 12px, 680px);
      max-height: calc(100dvh - 12px);
      border-radius: 18px;
    }

    .share-shell {
      max-height: calc(100dvh - 12px);
    }

    .share-heading,
    .source-region,
    .quality-panel,
    .share-actions {
      padding-right: 16px;
      padding-left: 16px;
    }

    .source-tabs,
    .browser-picker,
    .share-error {
      margin-right: 16px;
      margin-left: 16px;
    }

    .source-grid {
      grid-template-columns: 1fr;
    }

    .option-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .source-preview {
      min-height: 120px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spinner {
      animation-duration: 1.8s;
    }
  }
</style>
