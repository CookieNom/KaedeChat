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
  const selectedSource = $derived(
    visibleSources.find((source) => source.id === selectedSourceId) ?? null
  );

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

  function changeSourceTab(tab: 'application' | 'screen') {
    sourceTab = tab;
    if (!native) return;
    const next = sources.find((source) => {
      const kind = source.kind ?? (source.id.startsWith('window:') ? 'application' : 'screen');
      return kind === tab;
    });
    selectedSourceId = next?.id ?? null;
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

  async function share(browserSurface?: 'window' | 'browser' | 'monitor') {
    if (sharing) return;
    if (native && !securePicker && !selectedSource) {
      error = 'Choose a window or display to share.';
      return;
    }
    sharing = true;
    error = '';
    saveMediaQuality(preferences);
    try {
      await onShare(
        preferences,
        selectedSource?.id ??
          (!native
            ? `browser:${browserSurface ?? (sourceTab === 'application' ? 'window' : 'monitor')}`
            : null)
      );
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
    <h2 id="screen-share-title" class="visually-hidden">Choose what to share</h2>
    <button class="close-button" type="button" aria-label="Close" disabled={sharing} onclick={close}
      >×</button
    >

    <nav class="source-tabs" aria-label="Screen-share source type">
      <button
        type="button"
        data-initial-focus
        class:active={sourceTab === 'application'}
        aria-pressed={sourceTab === 'application'}
        onclick={() => changeSourceTab('application')}
      >
        <Icon name="image" size={21} /> Applications
      </button>
      <button
        type="button"
        class:active={sourceTab === 'screen'}
        aria-pressed={sourceTab === 'screen'}
        onclick={() => changeSourceTab('screen')}
      >
        <Icon name="screen" size={21} /> Entire Screen
      </button>
    </nav>

    <main class="source-region" aria-live="polite">
      {#if native}
        {#if loadingSources}
          <div class="source-message"><span class="spinner"></span>Finding shareable sources…</div>
        {:else if securePicker}
          <button class="system-picker-card" type="button" onclick={() => void share()}>
            <span class="picker-illustration"><Icon name="shield" size={54} /></span>
            <span class="picker-copy">
              <strong>Open the secure system picker</strong>
              <small>
                {nativeOs === 'macos'
                  ? 'macOS shows applications and displays in its privacy-protected chooser.'
                  : 'Your Wayland session shows applications and displays through the desktop portal.'}
              </small>
            </span>
          </button>
        {:else if visibleSources.length === 0}
          <div class="empty-sources">
            <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={38} />
            <strong>No {sourceTab === 'application' ? 'applications' : 'screens'} found</strong>
            <span>Open a window or connect a display, then refresh.</span>
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
                ondblclick={() => void share()}
              >
                <span class="source-preview">
                  {#if thumbnails[source.id]}
                    <img src={thumbnails[source.id]} alt="" />
                  {:else}
                    <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={42} />
                  {/if}
                  {#if selectedSourceId === source.id}
                    <span class="selected-check"><Icon name="check" size={16} /></span>
                  {/if}
                </span>
                <span class="source-label">
                  <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={18} />
                  <strong>{source.label.replace(/^(Window|Display):\s*/, '')}</strong>
                </span>
              </button>
            {/each}
          </div>
        {/if}
      {:else}
        <div class="browser-source-grid" class:single-source={sourceTab === 'screen'}>
          <button
            class="browser-source-card selected"
            type="button"
            onclick={() => void share(sourceTab === 'application' ? 'window' : 'monitor')}
          >
            <span class="browser-preview" class:screen-preview={sourceTab === 'screen'}>
              <span class="preview-window preview-window-back"></span>
              <span class="preview-window preview-window-front">
                <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={52} />
              </span>
              <span class="privacy-chip"><Icon name="shield" size={15} /> Protected picker</span>
            </span>
            <span class="source-label">
              <Icon name={sourceTab === 'application' ? 'image' : 'screen'} size={18} />
              <span>
                <strong
                  >{sourceTab === 'application' ? 'Application window' : 'Entire screen'}</strong
                >
                <small>Your browser will show the available sources</small>
              </span>
            </span>
          </button>
          {#if sourceTab === 'application'}
            <button class="browser-source-card" type="button" onclick={() => void share('browser')}>
              <span class="browser-preview tab-preview">
                <span class="preview-window preview-window-front">
                  <Icon name="globe" size={52} />
                </span>
                <span class="privacy-chip"><Icon name="shield" size={15} /> Protected picker</span>
              </span>
              <span class="source-label">
                <Icon name="globe" size={18} />
                <span>
                  <strong>Browser tab</strong>
                  <small>Share one tab without exposing your desktop</small>
                </span>
              </span>
            </button>
          {/if}
        </div>
        <p class="browser-privacy">
          <Icon name="shield" size={16} /> Kaede cannot inspect window titles or previews until you approve
          a source in your browser.
        </p>
      {/if}
    </main>

    {#if error}<p class="share-error" role="alert">{error}</p>{/if}

    <footer class="stream-footer">
      <div class="quality-summary">
        <strong>{selectedProfile.label}</strong>
        <span>
          {selectedProfile.description} <b>•</b>
          {selectedProfile.height ? `${selectedProfile.height}p` : 'Source'} <b>•</b>
          {selectedProfile.frameRate} FPS
        </span>
      </div>

      <div class="footer-controls">
        <div class="quick-quality" aria-label="Quick stream quality">
          <button
            type="button"
            class:active={preferences.screenProfile === 'data_saver' ||
              preferences.screenProfile === 'smooth'}
            aria-label="Standard definition, 720p at 30 frames per second"
            onclick={() => (preferences.screenProfile = 'smooth')}>SD</button
          >
          <button
            type="button"
            class:active={preferences.screenProfile === 'sharp'}
            aria-label="High definition, 1080p at 30 frames per second"
            onclick={() => (preferences.screenProfile = 'sharp')}>HD</button
          >
        </div>

        <details class="quality-settings">
          <summary aria-label="Advanced stream settings"><Icon name="settings" size={23} /></summary
          >
          <div class="settings-card">
            <header>
              <div>
                <strong>Stream settings</strong>
                <span>Video quality and outgoing microphone audio</span>
              </div>
            </header>

            <fieldset>
              <legend>Resolution and frame rate</legend>
              <div class="option-grid video-options">
                {#each SCREEN_SHARE_PROFILES as profile (profile.id)}
                  <label class:selected={preferences.screenProfile === profile.id}>
                    <input
                      type="radio"
                      name="screen-quality"
                      value={profile.id}
                      checked={preferences.screenProfile === profile.id}
                      onchange={() =>
                        (preferences.screenProfile = profile.id as ScreenShareProfileId)}
                    />
                    <strong>{profile.height ? `${profile.height}p` : 'Source'}</strong>
                    <span>{profile.frameRate} FPS</span>
                  </label>
                {/each}
              </div>
            </fieldset>

            <fieldset>
              <legend>Outgoing audio bitrate</legend>
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
                    <strong>{quality.maxBitrate / 1000} kbps</strong>
                    <span>{quality.label}</span>
                  </label>
                {/each}
              </div>
              <p class="bitrate-note">Adaptive upper limit; silence and congestion use less.</p>
            </fieldset>

            <label class="audio-toggle">
              <span>
                <strong>Share computer audio</strong>
                <small>
                  {native
                    ? 'Unavailable in the native desktop capture pipeline.'
                    : 'Availability depends on your browser and selected source.'}
                </small>
              </span>
              <input type="checkbox" bind:checked={preferences.shareAudio} disabled={native} />
            </label>
          </div>
        </details>

        <button class="cancel-button" type="button" disabled={sharing} onclick={close}
          >Cancel</button
        >
        <button
          class="share-button"
          type="button"
          disabled={sharing || loadingSources || (native && !securePicker && !selectedSource)}
          onclick={() => void share()}
        >
          <Icon name="screen" size={18} />
          {sharing ? 'Starting…' : native && !securePicker ? 'Go Live' : 'Choose source'}
        </button>
      </div>
    </footer>
  </section>
</dialog>

<style>
  .share-dialog {
    width: min(1120px, calc(100vw - 32px));
    max-height: min(820px, calc(100dvh - 32px));
    padding: 0;
    overflow: visible;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: 22px;
    background: var(--surface);
    box-shadow: var(--shadow-lg);
  }

  .share-dialog::backdrop {
    background: rgb(0 0 0 / 74%);
    backdrop-filter: blur(5px);
  }

  .share-shell {
    position: relative;
    display: grid;
    max-height: min(820px, calc(100dvh - 32px));
    grid-template-rows: auto minmax(260px, 1fr) auto auto;
    overflow: visible;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .close-button {
    position: absolute;
    z-index: 4;
    top: -13px;
    right: -13px;
    display: grid;
    width: 34px;
    height: 34px;
    padding: 0;
    place-items: center;
    color: var(--text-muted);
    border: 1px solid var(--line-strong);
    border-radius: 50%;
    background: var(--surface-raised);
    box-shadow: 0 8px 18px rgb(0 0 0 / 28%);
    font-size: 1.45rem;
    line-height: 1;
    cursor: pointer;
  }

  .source-tabs {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    margin: 22px 24px 0;
    padding: 5px;
    border-radius: 16px;
    background: var(--rail);
  }

  .source-tabs button {
    display: flex;
    min-height: 48px;
    align-items: center;
    justify-content: center;
    gap: 10px;
    color: var(--rail-text);
    border: 0;
    border-radius: 12px;
    background: transparent;
    font-size: 0.96rem;
    font-weight: 720;
    cursor: pointer;
    opacity: 0.62;
  }

  .source-tabs button.active {
    background: var(--rail-hover);
    box-shadow: 0 3px 12px rgb(0 0 0 / 22%);
    opacity: 1;
  }

  .source-region {
    min-height: 0;
    overflow: auto;
    padding: 20px 24px 24px;
    scrollbar-gutter: stable;
  }

  .source-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px 20px;
  }

  .source-card,
  .browser-source-card,
  .system-picker-card {
    min-width: 0;
    padding: 0;
    color: var(--text);
    text-align: left;
    border: 2px solid transparent;
    background: transparent;
    cursor: pointer;
  }

  .source-card,
  .browser-source-card {
    display: grid;
    gap: 9px;
    border-radius: 15px;
  }

  .source-card:hover .source-preview,
  .browser-source-card:hover .browser-preview {
    border-color: var(--line-strong);
    transform: translateY(-1px);
  }

  .source-card.selected,
  .browser-source-card.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  }

  .source-preview,
  .browser-preview {
    position: relative;
    display: grid;
    width: 100%;
    aspect-ratio: 16 / 8.7;
    place-items: center;
    overflow: hidden;
    color: var(--text-muted);
    border: 1px solid var(--line-soft);
    border-radius: 12px;
    background: #151515;
    transition: 140ms ease;
  }

  .source-preview img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #111;
  }

  .source-label {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 9px;
    padding: 0 10px 10px;
  }

  .source-label > strong,
  .source-label > span {
    min-width: 0;
  }

  .source-label > strong,
  .source-label span > strong {
    display: block;
    overflow: hidden;
    font-size: 0.86rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-label small {
    display: block;
    margin-top: 2px;
    color: var(--text-muted);
    font-size: 0.72rem;
  }

  .selected-check {
    position: absolute;
    top: 10px;
    right: 10px;
    display: grid;
    width: 27px;
    height: 27px;
    place-items: center;
    color: var(--on-accent);
    border: 2px solid var(--surface);
    border-radius: 50%;
    background: var(--accent);
  }

  .browser-source-grid {
    display: grid;
    grid-template-columns: minmax(300px, 1fr) minmax(220px, 0.58fr);
    min-height: 330px;
  }

  .browser-source-grid.single-source .browser-source-card {
    grid-column: 1;
  }

  .browser-preview {
    min-height: 255px;
    background:
      radial-gradient(
        circle at 50% 42%,
        color-mix(in srgb, var(--accent) 18%, transparent),
        transparent 43%
      ),
      #151515;
  }

  .preview-window {
    position: absolute;
    display: grid;
    place-items: center;
    border: 1px solid color-mix(in srgb, var(--text-muted) 42%, transparent);
    border-radius: 8px;
    background: var(--surface-raised);
    box-shadow: 0 14px 30px rgb(0 0 0 / 36%);
  }

  .preview-window::before {
    position: absolute;
    top: 0;
    right: 0;
    left: 0;
    height: 18px;
    border-bottom: 1px solid var(--line-soft);
    background: var(--surface-subtle);
    content: '';
  }

  .preview-window-back {
    width: 55%;
    height: 53%;
    transform: translate(18%, -12%) rotate(3deg);
    opacity: 0.55;
  }

  .preview-window-front {
    width: 60%;
    height: 58%;
    color: var(--accent-text);
    transform: translate(-7%, 7%);
  }

  .screen-preview .preview-window-back {
    display: none;
  }

  .tab-preview .preview-window-front {
    width: 68%;
    height: 62%;
    color: var(--accent-text);
    transform: none;
  }

  .screen-preview .preview-window-front {
    width: 68%;
    height: 62%;
    border-width: 5px;
    border-color: var(--surface-raised);
    border-radius: 5px;
    transform: none;
  }

  .screen-preview .preview-window-front::after {
    position: absolute;
    bottom: -18px;
    width: 26%;
    height: 13px;
    border-top: 4px solid var(--surface-raised);
    content: '';
  }

  .privacy-chip {
    position: absolute;
    right: 12px;
    bottom: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 9px;
    color: var(--text-soft);
    border: 1px solid var(--line-soft);
    border-radius: 999px;
    background: color-mix(in srgb, var(--surface) 90%, transparent);
    font-size: 0.68rem;
    font-weight: 700;
  }

  .browser-privacy {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 14px 0 0;
    color: var(--text-muted);
    font-size: 0.74rem;
  }

  .system-picker-card,
  .empty-sources,
  .source-message {
    min-height: 330px;
  }

  .system-picker-card {
    display: grid;
    grid-template-columns: minmax(300px, 1fr) minmax(220px, 0.58fr);
    width: 100%;
  }

  .picker-illustration {
    display: grid;
    min-height: 300px;
    place-items: center;
    color: var(--accent-text);
    border: 2px solid var(--accent);
    border-radius: 14px;
    background:
      radial-gradient(circle, color-mix(in srgb, var(--accent) 18%, transparent), transparent 52%),
      #151515;
  }

  .picker-copy {
    align-self: end;
    padding: 14px 12px;
  }

  .picker-copy strong,
  .picker-copy small {
    display: block;
  }

  .picker-copy small {
    margin-top: 4px;
    color: var(--text-muted);
  }

  .empty-sources,
  .source-message {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 9px;
    color: var(--text-muted);
    text-align: center;
  }

  .empty-sources strong {
    color: var(--text);
  }

  .spinner {
    width: 20px;
    height: 20px;
    border: 2px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 800ms linear infinite;
  }

  .share-error {
    margin: 0 24px 12px;
    padding: 10px 12px;
    color: var(--danger);
    border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--line));
    border-radius: 10px;
    background: var(--danger-soft);
    font-size: 0.78rem;
  }

  .stream-footer {
    position: relative;
    display: flex;
    min-height: 92px;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    padding: 16px 24px;
    border-top: 1px solid var(--line-soft);
    border-radius: 0 0 22px 22px;
    background: var(--surface-subtle);
  }

  .quality-summary {
    display: grid;
    min-width: 0;
    gap: 4px;
  }

  .quality-summary strong {
    font-size: 1rem;
  }

  .quality-summary span {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.79rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .quality-summary b {
    padding: 0 3px;
    color: var(--line-strong);
  }

  .footer-controls {
    display: flex;
    flex: 0 0 auto;
    align-items: stretch;
    gap: 9px;
  }

  .quick-quality {
    display: grid;
    grid-template-columns: 1fr 1fr;
    padding: 4px;
    border-radius: 13px;
    background: var(--rail);
  }

  .quick-quality button {
    min-width: 54px;
    padding: 0 14px;
    color: var(--text-muted);
    border: 0;
    border-radius: 9px;
    background: transparent;
    font-weight: 820;
    cursor: pointer;
  }

  .quick-quality button.active {
    color: var(--rail-text);
    background: var(--rail-hover);
  }

  .quality-settings {
    position: relative;
  }

  .quality-settings > summary {
    display: grid;
    width: 48px;
    height: 100%;
    min-height: 48px;
    padding: 0;
    place-items: center;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: 12px;
    background: var(--surface-raised);
    cursor: pointer;
    list-style: none;
  }

  .quality-settings > summary::-webkit-details-marker {
    display: none;
  }

  .quality-settings[open] > summary {
    color: var(--accent-text);
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .settings-card {
    position: absolute;
    z-index: 10;
    right: 0;
    bottom: calc(100% + 14px);
    display: grid;
    width: min(610px, calc(100vw - 48px));
    gap: 16px;
    padding: 18px;
    border: 1px solid var(--line-strong);
    border-radius: 16px;
    background: var(--surface-raised);
    box-shadow: 0 18px 50px rgb(0 0 0 / 42%);
  }

  .settings-card::after {
    position: absolute;
    right: 15px;
    bottom: -7px;
    width: 12px;
    height: 12px;
    border-right: 1px solid var(--line-strong);
    border-bottom: 1px solid var(--line-strong);
    background: var(--surface-raised);
    content: '';
    transform: rotate(45deg);
  }

  .settings-card header strong,
  .settings-card header span {
    display: block;
  }

  .settings-card header strong {
    font-size: 1rem;
  }

  .settings-card header span {
    margin-top: 2px;
    color: var(--text-muted);
    font-size: 0.74rem;
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
    font-size: 0.7rem;
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
    gap: 2px;
    min-width: 0;
    padding: 10px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface-subtle);
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
    margin: 6px 0 0;
    color: var(--text-muted);
    font-size: 0.68rem;
  }

  .audio-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 10px 12px;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    background: var(--surface-subtle);
  }

  .audio-toggle span,
  .audio-toggle strong,
  .audio-toggle small {
    display: block;
  }

  .audio-toggle small {
    margin-top: 2px;
    color: var(--text-muted);
    font-size: 0.7rem;
  }

  .audio-toggle input {
    width: 18px;
    height: 18px;
    accent-color: var(--accent);
  }

  .cancel-button,
  .share-button,
  .secondary {
    display: inline-flex;
    min-height: 48px;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 0 16px;
    border: 1px solid var(--line);
    border-radius: 12px;
    font-weight: 760;
    cursor: pointer;
  }

  .cancel-button,
  .secondary {
    color: var(--text);
    background: var(--surface-raised);
  }

  .share-button {
    color: var(--on-accent);
    border-color: var(--accent);
    background: var(--accent);
  }

  .share-button:hover {
    background: var(--accent-hover);
  }

  button:disabled,
  input:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 760px) {
    .share-dialog {
      width: calc(100vw - 12px);
      max-height: calc(100dvh - 12px);
      border-radius: 17px;
    }

    .share-shell {
      max-height: calc(100dvh - 12px);
      grid-template-rows: auto minmax(210px, 1fr) auto auto;
    }

    .source-tabs {
      margin: 12px 12px 0;
    }

    .source-tabs button {
      min-height: 44px;
      font-size: 0.82rem;
    }

    .source-region {
      padding: 12px;
    }

    .source-grid,
    .browser-source-grid,
    .system-picker-card {
      grid-template-columns: 1fr;
    }

    .browser-source-grid,
    .system-picker-card,
    .empty-sources,
    .source-message {
      min-height: 240px;
    }

    .browser-preview,
    .picker-illustration {
      min-height: 210px;
    }

    .stream-footer {
      align-items: stretch;
      flex-direction: column;
      gap: 12px;
      padding: 13px;
      border-radius: 0 0 17px 17px;
    }

    .footer-controls {
      display: grid;
      grid-template-columns: 1fr auto auto;
    }

    .cancel-button {
      display: none;
    }

    .settings-card {
      position: fixed;
      right: 12px;
      bottom: 108px;
      left: 12px;
      width: auto;
      max-height: calc(100dvh - 138px);
      overflow: auto;
    }

    .settings-card::after {
      display: none;
    }

    .option-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spinner {
      animation-duration: 1.8s;
    }

    .source-preview,
    .browser-preview {
      transition: none;
    }
  }
</style>
