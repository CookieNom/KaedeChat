<script lang="ts">
  import { onDestroy } from 'svelte';
  import { CODECS, VIDEO_CODECS } from '$lib/voice/video-codecs';
  import { probeVideoCodec, type VideoCodecProbe } from '$lib/voice/quality';

  let running = $state(false);
  let rows = $state<VideoCodecProbe[]>([]);
  let status = $state('Ready');
  let size = $state('1280x720');
  let camera = $state<MediaStream>();
  let requestingCamera = $state(false);
  let cameraError = $state('');
  let closed = false;
  let controller: AbortController | undefined;
  const cases = CODECS.flatMap((codec) =>
    VIDEO_CODECS[codec].modes.map((mode) => ({ codec, mode }))
  );
  onDestroy(() => {
    closed = true;
    controller?.abort();
    releaseCamera();
  });

  function releaseCamera() {
    camera?.getTracks().forEach((track) => track.stop());
    camera = undefined;
  }

  async function allowHardwareNames() {
    if (requestingCamera || running || camera) return;
    requestingCamera = true;
    cameraError = '';
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      if (closed) stream.getTracks().forEach((track) => track.stop());
      else camera = stream;
    } catch {
      cameraError = 'Camera access is unavailable. You can still run tests with names hidden.';
    } finally {
      requestingCamera = false;
    }
  }

  async function run() {
    if (running || requestingCamera) return;
    running = true;
    rows = [];
    controller = new AbortController();
    const [width, height] = size.split('x').map(Number);
    try {
      for (const { codec, mode } of cases) {
        if (controller.signal.aborted) break;
        status = `Testing ${VIDEO_CODECS[codec].label} · ${mode} (${rows.length + 1}/${cases.length})`;
        const row = await probeVideoCodec(
          codec,
          {
            width,
            height,
            frameRate: 30,
            bitrate: width > 1920 ? 8_000_000 : 4_500_000,
            scalabilityMode: mode
          },
          controller.signal
        );
        rows = [...rows, row];
      }
      status = controller.signal.aborted ? 'Stopped' : 'Finished';
    } finally {
      running = false;
      releaseCamera();
    }
  }

  function download() {
    if (running || !rows.length) return;
    const url = URL.createObjectURL(
      new Blob(
        [
          JSON.stringify(
            {
              browser: navigator.userAgent,
              size,
              frameRate: 30,
              results: rows
            },
            null,
            2
          )
        ],
        { type: 'application/json' }
      )
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = 'kaede-video-capabilities.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function support(advertised: boolean, verified: boolean, hardware: boolean): string {
    return verified
      ? hardware
        ? 'Hardware verified'
        : 'Frames verified; hardware unproven'
      : advertised
        ? 'Advertised; unverified'
        : 'Not advertised';
  }
</script>

<svelte:head><title>Video capability probe · Kaede</title></svelte:head>

<main>
  <header>
    <p class="eyebrow">Kaede diagnostics</p>
    <h1>Video capability probe</h1>
    <p>
      Check which video codecs and scalability modes this browser can use. Tests send generated
      pixels between two local connections. No account or microphone is needed.
    </p>
    <p class="muted">
      This tests the browser engine, including inside the desktop app. Native call encoders are
      separate. A missing or hidden hardware name is never treated as proof.
    </p>
  </header>
  <details>
    <summary>Reveal hardware names</summary>
    <p class="muted">
      Some browsers hide encoder and decoder names without active camera access. You can optionally
      allow access during these tests. Camera images stay on this device; the tests still encode
      generated pixels. The camera turns off when the tests finish.
    </p>
    {#if camera}
      <p>Camera access enabled for hardware details.</p>
      <button onclick={releaseCamera} disabled={running}>Turn off camera</button>
    {:else}
      <button onclick={allowHardwareNames} disabled={requestingCamera || running}>
        {requestingCamera ? 'Waiting for camera permission…' : 'Allow camera access'}
      </button>
    {/if}
    {#if cameraError}<p role="alert">{cameraError}</p>{/if}
  </details>
  <div class="controls">
    <label
      >Video size
      <select bind:value={size} disabled={running}>
        <option value="1280x720">720p</option><option value="1920x1080">1080p</option>
        <option value="3840x2160">4K</option>
      </select>
    </label>
    <button class="primary" onclick={run} disabled={running || requestingCamera}
      >{running ? 'Testing…' : 'Run tests'}</button
    >
    {#if running}<button onclick={() => controller?.abort()}>Stop tests</button>{/if}
    <button onclick={download} disabled={running || !rows.length}>Download results</button>
  </div>
  <p class="status" role="status" aria-live="polite">{status}</p>
  <p class="muted">
    L1 has one resolution layer; L3 has three. T1 and T3 mean one or three frame-rate layers. Each
    requested mode must match the mode actually encoded. A loopback needs both an encoder and a
    decoder; unverified does not mean unsupported.
  </p>
  <div class="results">
    {#each rows as row (`${row.codec}/${row.mode}`)}
      <article>
        <h2>{VIDEO_CODECS[row.codec].label} <span>{row.mode}</span></h2>
        <dl>
          <div>
            <dt>Encode</dt>
            <dd>{support(row.advertisedEncode, row.encoded, row.hardwareEncode)}</dd>
          </div>
          <div>
            <dt>Encoder name</dt>
            <dd>{row.encoder || 'Not reported'}</dd>
          </div>
          <div>
            <dt>Decode</dt>
            <dd>{support(row.advertisedDecode, row.decoded, row.hardwareDecode)}</dd>
          </div>
          <div>
            <dt>Decoder name</dt>
            <dd>{row.decoder || 'Not reported'}</dd>
          </div>
          <div>
            <dt>Actual mode</dt>
            <dd>{row.actualMode || 'Not reported'}</dd>
          </div>
        </dl>
        <p class="muted">{row.detail}</p>
      </article>
    {/each}
  </div>
</main>

<style>
  main {
    max-width: 1080px;
    margin: auto;
    padding: 48px 24px;
    color: var(--text);
  }
  header {
    max-width: 760px;
  }
  h1 {
    font-size: clamp(1.8rem, 5vw, 2.5rem);
    line-height: 1.2;
    margin: 8px 0 20px;
  }
  p {
    line-height: 1.6;
  }
  .eyebrow,
  .muted {
    color: var(--text-soft);
  }
  .eyebrow {
    font-weight: 600;
    margin: 0;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 12px;
    margin: 28px 0 16px;
  }
  details {
    margin-top: 24px;
    max-width: 760px;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
    padding: 8px 0;
  }
  label {
    display: grid;
    gap: 8px;
    font-weight: 600;
  }
  button,
  select {
    min-height: 44px;
    padding: 10px 16px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--control-surface);
    color: var(--text);
    font: inherit;
  }
  button {
    cursor: pointer;
    font-weight: 600;
  }
  button.primary {
    background: var(--accent);
    color: var(--text-inverse);
    border-color: var(--accent);
  }
  button:disabled,
  select:disabled {
    opacity: 0.5;
    cursor: default;
  }
  button:focus-visible,
  select:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }
  .status {
    font-weight: 600;
  }
  .results {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr));
    gap: 16px;
    margin-top: 24px;
  }
  article {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 20px;
    min-width: 0;
  }
  h2 {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 12px;
    margin: 0 0 20px;
    font-size: 1.1rem;
  }
  h2 span {
    color: var(--text-soft);
    font-weight: 400;
  }
  dl {
    margin: 0;
  }
  dl div {
    display: grid;
    grid-template-columns: 110px 1fr;
    gap: 12px;
    margin: 12px 0;
  }
  dt {
    color: var(--text-soft);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  article p {
    margin-bottom: 0;
    font-size: 0.9rem;
  }
  @media (max-width: 480px) {
    main {
      padding: 24px 16px;
    }
    article {
      padding: 16px;
    }
    .controls > * {
      flex: 1 1 140px;
    }
    dl div {
      grid-template-columns: 95px 1fr;
    }
  }
</style>
